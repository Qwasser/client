import os

from wandb import util

class ArtifactsCache(object):
    def __init__(self, cache_dir):
        util.mkdir_exists_ok(cache_dir)
        self._cache_dir = cache_dir

    def get_artifact_dir(self, artifact_type, artifact_digest):
        dirname = os.path.join(self._cache_dir, artifact_type, artifact_digest, 'artifact')
        util.mkdir_exists_ok(dirname)
        return dirname

    def get_artifact_external_dir(self, artifact_type, artifact_digest):
        dirname = os.path.join(self._cache_dir, artifact_type, artifact_digest, 'external')
        util.mkdir_exists_ok(dirname)
        return dirname

    def get_artifact_write_dir(self, artifact_type):
        dirname = os.path.join(self._cache_dir, artifact_type, 'creating', util.generate_id())
        return dirname

    def finish_artifact_write_dir(self, write_dir, artifact_type, artifact_digest):
        # TODO remove target dir
        shutil.move(write_dir, self.get_artifact_dir(artifact_type, artifact_digest))

_artifacts_cache = None

def get_artifacts_cache():
    global _artifacts_cache
    if _artifacts_cache is None:
        _artifacts_cache = ArtifactsCache(os.path.expanduser('~/.cache/wandb/artifacts'))
    return _artifacts_cache
