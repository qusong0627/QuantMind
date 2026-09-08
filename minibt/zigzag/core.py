

def __bootstrap__():
    global __bootstrap__, __loader__, __file__
    import sys
    import pkg_resources
    import importlib.util
    # print(__name__)
    __file__ = pkg_resources.resource_filename(
        __name__, 'core.cp312-win_amd64.pyd')
    __loader__ = None
    del __bootstrap__, __loader__
    spec = importlib.util.spec_from_file_location(__name__, __file__)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)


__bootstrap__()
