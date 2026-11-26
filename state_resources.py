"""Release database handles and process locks after failed initialization."""

from functools import wraps


def cleanup_on_error(initialize):
    @wraps(initialize)
    def guarded(self, *args, **kwargs):
        try:
            return initialize(self, *args, **kwargs)
        except BaseException:
            for name in ('db', 'lock'):
                resource = getattr(self, name, None)
                if resource is not None:
                    resource.close()
            raise
    return guarded
