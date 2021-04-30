"""
sync.
"""

from __future__ import print_function

import datetime
import fnmatch
import json
import os
import shutil
import sys
import threading
import time

from six.moves.urllib.parse import quote as url_quote
import wandb
from wandb.compat import tempfile
from wandb.proto import wandb_internal_pb2  # type: ignore
from wandb.sdk.internal import run as internal_run
from wandb.util import check_and_warn_old, to_forward_slash_path


# TODO: consolidate dynamic imports
PY3 = sys.version_info.major == 3 and sys.version_info.minor >= 6
if PY3:
    from wandb.sdk.internal import datastore
    from wandb.sdk.internal import sender
    from wandb.sdk.internal import tb_watcher
else:
    from wandb.sdk_py27.internal import datastore
    from wandb.sdk_py27.internal import sender
    from wandb.sdk_py27.internal import tb_watcher

WANDB_SUFFIX = ".wandb"
SYNCED_SUFFIX = ".synced"
TFEVENT_SUBSTRING = ".tfevents."
TMPDIR = tempfile.TemporaryDirectory()


class _LocalRun(object):
    def __init__(self, path, synced=None):
        self.path = path
        self.synced = synced
        self.offline = os.path.basename(path).startswith("offline-")
        self.datetime = datetime.datetime.strptime(
            os.path.basename(path).split("run-")[1].split("-")[0], "%Y%m%d_%H%M%S"
        )

    def __str__(self):
        return self.path


class SyncThread(threading.Thread):
    def __init__(
        self,
        sync_list,
        project=None,
        entity=None,
        run_id=None,
        view=None,
        verbose=None,
        mark_synced=None,
        app_url=None,
        sync_tensorboard=None,
    ):
        threading.Thread.__init__(self)
        # mark this process as internal
        wandb._set_internal_process(disable=True)
        self._sync_list = sync_list
        self._project = project
        self._entity = entity
        self._run_id = run_id
        self._view = view
        self._verbose = verbose
        self._mark_synced = mark_synced
        self._app_url = app_url
        self._sync_tensorboard = sync_tensorboard

    def _parse_pb(self, data, exit_pb=None):
        pb = wandb_internal_pb2.Record()
        pb.ParseFromString(data)
        record_type = pb.WhichOneof("record_type")
        if self._view:
            if self._verbose:
                print("Record:", pb)
            else:
                print("Record:", record_type)
            return pb, exit_pb, True
        if record_type == "run":
            if self._run_id:
                pb.run.run_id = self._run_id
            if self._project:
                pb.run.project = self._project
            if self._entity:
                pb.run.entity = self._entity
            pb.control.req_resp = True
        elif record_type == "exit":
            exit_pb = pb
            return pb, exit_pb, True
        elif record_type == "final":
            assert exit_pb, "final seen without exit"
            pb = exit_pb
            exit_pb = None
        return pb, exit_pb, False

    def _find_tfevent_files(self, sync_item):
        tb_event_files = 0
        tb_logdirs = []
        tb_root = None
        if self._sync_tensorboard:
            if os.path.isdir(sync_item):
                files = []
                for dirpath, _, _files in os.walk(sync_item):
                    for f in _files:
                        if TFEVENT_SUBSTRING in f:
                            files.append(os.path.join(dirpath, f))
                for tfevent in files:
                    tb_event_files += 1
                    tb_dir = os.path.dirname(os.path.abspath(tfevent))
                    if tb_dir not in tb_logdirs:
                        tb_logdirs.append(tb_dir)
                if len(tb_logdirs) > 0:
                    tb_root = to_forward_slash_path(
                os.path.dirname(os.path.commonprefix(tb_logdirs)))
            elif TFEVENT_SUBSTRING in sync_item:
                tb_root = os.path.dirname(os.path.abspath(sync_item))
                tb_logdirs.append(tb_root)
                tb_event_files = 1
        return tb_event_files, tb_logdirs, tb_root

    def _send_tensorboard(self, tb_root, tb_logdirs, send_manager):
        if self._entity is None:
            viewer, server_info = send_manager._api.viewer_server_info()
            self._entity = viewer.get("entity")
        proto_run = wandb_internal_pb2.RunRecord()
        proto_run.run_id = self._run_id or wandb.util.generate_id()
        proto_run.project = self._project or wandb.util.auto_project_name(None)
        proto_run.entity = self._entity

        url = "{}/{}/{}/runs/{}".format(
            self._app_url,
            url_quote(proto_run.entity),
            url_quote(proto_run.project),
            url_quote(proto_run.run_id),
        )
        print("Syncing: %s ..." % url)
        sys.stdout.flush()
        record = send_manager._interface._make_record(run=proto_run)
        send_manager.send(record)
        settings = wandb.Settings(
            root_dir=TMPDIR.name,
            run_id=proto_run.run_id,
            _start_datetime=datetime.datetime.now(),
            _start_time=time.time(),
        )

        def datatypes_cb(fname: str) -> None:
            files = dict(files=[(fname, "now")])
            self._tbwatcher._interface.publish_files(files)

        run = internal_run.InternalRun(proto_run, settings=settings, datatypes_cb=datatypes_cb)
        watcher = tb_watcher.TBWatcher(
            settings, proto_run, send_manager._interface, True, True
        )
        for tb in tb_logdirs:
            watcher.add(tb, True, tb_root)
            sys.stdout.flush()
        watcher.finish()
        # send all of our records like a boss
        while not send_manager._interface.record_q.empty():
            data = send_manager._interface.record_q.get(block=True)
            if len(data.history.ListFields()) != 0:
                item = data.history.item.add()
                item.key = "_step"
                item.value_json = json.dumps(data.history.step.num)
            send_manager.send(data)
        for file_or_dir in os.listdir(watcher._consumer._internal_run.dir):
            if os.path.isdir(os.path.join(watcher._consumer._internal_run.dir, file_or_dir)):
                shutil.copytree(os.path.join(watcher._consumer._internal_run.dir, file_or_dir), os.path.join(TMPDIR.name, "files", file_or_dir))
            else:
                shutil.copy(os.path.join(watcher._consumer._internal_run.dir, file_or_dir), os.path.join(TMPDIR.name, "files"))
        sys.stdout.flush()
        send_manager.finish()

    def run(self):
        for sync_item in self._sync_list:
            tb_event_files, tb_logdirs, tb_root = self._find_tfevent_files(sync_item)
            if os.path.isdir(sync_item):
                files = os.listdir(sync_item)
                filtered_files = list(filter(lambda f: f.endswith(WANDB_SUFFIX), files))
                if tb_root is None and (
                    check_and_warn_old(files) or len(filtered_files) != 1
                ):
                    print("Skipping directory: {}".format(sync_item))
                    continue
                if len(filtered_files) > 0:
                    sync_item = os.path.join(sync_item, filtered_files[0])
            root_dir = os.path.dirname(sync_item)
            # If we're syncing tensorboard, let's use a tmpdir
            if tb_event_files > 0 and not sync_item.endswith(WANDB_SUFFIX):
                root_dir = TMPDIR.name
            sm = sender.SendManager.setup(root_dir)

            if tb_root is not None:
                if tb_event_files > 0 and sync_item.endswith(WANDB_SUFFIX):
                    wandb.termwarn(
                        "Found .wandb file, not streaming tensorboard metrics."
                    )
                else:
                    print(
                        "Found {} tfevent files in {}".format(tb_event_files, tb_root)
                    )
                    if len(tb_logdirs) > 3:
                        wandb.termwarn(
                            "Found {} directories containing tfevent files. "
                            "If these represent multiple experiments, sync them "
                            "individually or pass a list of paths."
                        )
                    self._send_tensorboard(tb_root, tb_logdirs, sm)
                    continue
            ds = datastore.DataStore()
            ds.open_for_scan(sync_item)

            # save exit for final send
            exit_pb = None
            shown = False

            while True:
                data = ds.scan_data()
                if data is None:
                    break
                pb, exit_pb, cont = self._parse_pb(data, exit_pb)
                if cont:
                    continue
                sm.send(pb)
                # send any records that were added in previous send
                while not sm._record_q.empty():
                    data = sm._record_q.get(block=True)
                    sm.send(data)

                if pb.control.req_resp:
                    result = sm._result_q.get(block=True)
                    result_type = result.WhichOneof("result_type")
                    if not shown and result_type == "run_result":
                        r = result.run_result.run
                        # TODO(jhr): hardcode until we have settings in sync
                        url = "{}/{}/{}/runs/{}".format(
                            self._app_url,
                            url_quote(r.entity),
                            url_quote(r.project),
                            url_quote(r.run_id),
                        )
                        print("Syncing: %s ..." % url, end="")
                        sys.stdout.flush()
                        shown = True
            sm.finish()
            if self._mark_synced and not self._view:
                synced_file = "{}{}".format(sync_item, SYNCED_SUFFIX)
                with open(synced_file, "w"):
                    pass
            print("done.")


class SyncManager:
    def __init__(
        self,
        project=None,
        entity=None,
        run_id=None,
        mark_synced=None,
        app_url=None,
        view=None,
        verbose=None,
        sync_tensorboard=None,
    ):
        self._sync_list = []
        self._thread = None
        self._project = project
        self._entity = entity
        self._run_id = run_id
        self._mark_synced = mark_synced
        self._app_url = app_url
        self._view = view
        self._verbose = verbose
        self._sync_tensorboard = sync_tensorboard

    def status(self):
        pass

    def add(self, p):
        self._sync_list.append(os.path.abspath(str(p)))

    def start(self):
        # create a thread for each file?
        self._thread = SyncThread(
            sync_list=self._sync_list,
            project=self._project,
            entity=self._entity,
            run_id=self._run_id,
            view=self._view,
            verbose=self._verbose,
            mark_synced=self._mark_synced,
            app_url=self._app_url,
            sync_tensorboard=self._sync_tensorboard,
        )
        self._thread.start()

    def is_done(self):
        return not self._thread.is_alive()

    def poll(self):
        time.sleep(1)
        return False


def get_runs(
    include_offline=None,
    include_online=None,
    include_synced=None,
    include_unsynced=None,
    exclude_globs=None,
    include_globs=None,
):
    # TODO(jhr): grab dir info from settings
    base = "wandb"
    if os.path.exists(".wandb"):
        base = ".wandb"
    if not os.path.exists(base):
        return ()
    all_dirs = os.listdir(base)
    dirs = []
    if include_offline:
        dirs += filter(lambda d: d.startswith("offline-run-"), all_dirs)
    if include_online:
        dirs += filter(lambda d: d.startswith("run-"), all_dirs)
    # find run file in each dir
    fnames = []
    for d in dirs:
        paths = os.listdir(os.path.join(base, d))
        if exclude_globs:
            paths = set(paths)
            for g in exclude_globs:
                paths = paths - set(fnmatch.filter(paths, g))
            paths = list(paths)
        if include_globs:
            new_paths = set()
            for g in include_globs:
                new_paths = new_paths.union(fnmatch.filter(paths, g))
            paths = list(new_paths)
        for f in paths:
            if f.endswith(WANDB_SUFFIX):
                fnames.append(os.path.join(base, d, f))
    filtered = []
    for f in fnames:
        dname = os.path.dirname(f)
        # TODO(frz): online runs are assumed to be synced, verify from binary log.
        if os.path.exists("{}{}".format(f, SYNCED_SUFFIX)) or os.path.basename(
            dname
        ).startswith("run-"):
            if include_synced:
                filtered.append(_LocalRun(dname, True))
        else:
            if include_unsynced:
                filtered.append(_LocalRun(dname, False))
    return tuple(filtered)


def get_run_from_path(path):
    return _LocalRun(path)
