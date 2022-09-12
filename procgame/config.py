import os
import yaml
import logging

values = None
"""The configuration data structure loaded from :file:`~/.pyprocgame/config.yaml` when this submodule is loaded."""

path = None
"""Path that the configuration data structure was loaded from, by :meth:`load`."""

def value_for_key_path(keypath, default=None):
    """Returns the value at the given *keypath* within :attr:`values`.
    
    A key path is a list of components delimited by dots (periods).  The components are interpreted
    as dictionary keys within the structure.
    For example, the key path ``'a.b'`` would yield ``'c'`` with the following :attr:`values` dictionary: ::
    
        {'a':{'b':'c'}}
    
    If the key path does not exist *default* will be returned.
    """
    v = values
    for component in keypath.split('.'):
        if v != None and hasattr(v,'has_key') and v.has_key(component):
            v = v[component]
        else:
            v = default
    return v

def load():
    global values, path
    logger = logging.getLogger('game.config')
    config_path = os.path.expanduser('./config/config.yaml')
    if not os.path.exists(config_path):
        logger.info('SkeletonGame configuration not found at ' + config_path)
        config_path = os.path.expanduser('./config.yaml')
        if not os.path.exists(config_path):
            logger.info('SkeletonGame configuration not found at ' + config_path)
            config_path = os.path.expanduser('~/.pyprocgame/config.yaml')
            if not os.path.exists(config_path):
                logger.info('SkeletonGame configuration not found at ' + config_path)
                logger.warning('SkeletonGame configuration not found')
                return
    logger.info('SkeletonGame configuration found at %s' % config_path)
    try:
        values = yaml.load(open(config_path, 'r'))
    except Exception, e:
        logger.error('Error loading SkeletonGame configuration file %s: %s', config_path, e)

load()
