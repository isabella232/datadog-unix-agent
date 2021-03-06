# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache License Version 2.0.
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2018 Datadog, Inc.

import os
import copy
import yaml
import logging
import decimal
from collections import (
    defaultdict,
    OrderedDict,
)

from .providers import ConfigProvider


log = logging.getLogger(__name__)


class Config(object):

    DEFAULT_CONF_NAME = "datadog.yaml"
    DEFAULT_ENV_PREFIX = "DD_"

    def __init__(self, conf_name=DEFAULT_CONF_NAME, env_prefix=DEFAULT_ENV_PREFIX):
        self.search_paths = OrderedDict()
        self.conf_name = conf_name
        self.env_prefix = env_prefix
        self.env_bindings = set()
        self.data = {}
        self.defaults = {}
        self._loaded_config = None

        self._providers = {}
        self._check_configs = defaultdict(dict)

    def __getitem__(self, key):
        try:
            item = self.data[key]

            if isinstance(item, dict) and key in self.defaults and isinstance(self.defaults[key], dict):
                # merge the configs
                merge_config = copy.deepcopy(self.defaults[key])
                merge_config.update(item)
                item = merge_config
        except KeyError:
            item = self.defaults[key]

        return item

    def __setitem__(self, key, value):
        self.set(key, value)

    def __delitem__(self, key):
        self.reset(key)

    def set_default(self, key, value):
        if not isinstance(key, list):
            self.defaults[key] = value
        else:
            node = self.defaults
            for k in key[:-1]:
                if k not in node:
                    node[k] = {}
                node = node[k]

            node[key[-1]] = value

    def set(self, key, value):
        self.data[key] = value

    def reset(self, key):
        del self.data[key]

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def get_loaded_config(self):
        return self._loaded_config

    def add_search_path(self, search_path):
        # we're just using the ordered dict as a set:
        # the order in which paths are added matters.
        self.search_paths[search_path] = None

    def load(self, reload=True):
        loaded = False
        if self.search_paths:
            for path in self.search_paths.keys():
                conf_path = os.path.join(path, self.conf_name)
                if os.path.isfile(conf_path):
                    with open(conf_path, "r") as f:
                        self.data = yaml.safe_load(f)

                    log.info("loaded config from: %s", conf_path)
                    self._loaded_config = conf_path
                    loaded = True

                    break
            else:
                log.error("Could not find %s in search_paths: %s", self.conf_name, self.search_paths)

        conf_path_override_key = self.env_prefix + 'conf_path'.upper()
        for env_var in self.env_bindings:
            key = self.env_prefix + env_var
            overridden = False
            if key in os.environ:
                self.env_override(key, env_var)
                overridden = True
            elif key.upper() in os.environ:
                self.env_override(key.upper(), env_var)
                overridden = True

            if overridden and key.upper() == conf_path_override_key:
                loaded = False  # we need to override the default conf_path discard config

        # load again if conf_path specified with env var
        if not loaded and self.get('conf_path') and reload:
            self.add_search_path(self.get('conf_path'))
            self.load(reload=False)
        else:
            self.validate()

    def bind_env(self, key):
        self.env_bindings.add(key)

    def bind_env_and_set_default(self, key, path, value):
        if isinstance(value, dict):
            if not isinstance(path, list):
                path = [path]

            for k, v in value.items():
                self.bind_env_and_set_default("{}_{}".format(key, k), path + [k], v)
        else:
            self.bind_env(key)
            self.set_default(path, value)

    def env_var_namespaces(self, env_var):
        namespaces = [(env_var, '')]
        split = env_var.split('_')
        for i in range(len(split)):
            namespaces.append(('_'.join(split[:i]), '_'.join(split[i:])))

        return namespaces

    def env_override(self, env_var, key, path=[]):
        key_path = list(path)
        data = self.data
        defaults = self.defaults
        for p in key_path:
            data = data.get(p, {})
            if not data:
                break
        for p in key_path:
            defaults = defaults.get(p, {})
            if not defaults:
                break

        if not (data or defaults):
            log.warn("key prefix unexpectedly unavailable in configurations")
            return False

        for key_prefix, key_suffix in self.env_var_namespaces(key):
            if key_prefix in data or key_prefix in defaults:
                if key_prefix not in data:
                    data[key_prefix] = copy.deepcopy(defaults[key_prefix])

                if key_suffix:
                    key_path.append(key_prefix)
                    return self.env_override(env_var, key_suffix, path=key_path)
                else:
                    try:
                        data[key_prefix] = os.environ[env_var]
                        return True
                    except TypeError:
                        log.warn("unable to override: %s", env_var)
                        return False

        return False

    def validate(self):
        self.validate_histogram_aggregates()
        self.validate_histogram_percentiles()

    def validate_histogram_aggregates(self):
        aggregates_config = self.data.get('histogram_aggregates')

        if not aggregates_config:
            return
        if aggregates_config and not isinstance(aggregates_config, list):
            log.exception("histogram_aggregates should be a list - ignoring")
            self.data.pop('histogram_aggregates')
            return

        result = []
        valid_values = ['min', 'max', 'median', 'avg', 'sum', 'count']

        for val in aggregates_config:
            try:
                val = val.strip()
                if val not in valid_values:
                    log.warning("Ignored histogram aggregate %s, invalid", val)
                    continue
                else:
                    result.append(val)
            except Exception:
                log.exception("Error when parsing histogram aggregate %s, invalid", val)

        self.data['histogram_aggregates'] = result

    def validate_histogram_percentiles(self):
        percentiles_config = self.data.get('histogram_percentiles')

        if not percentiles_config:
            return
        elif percentiles_config and not isinstance(percentiles_config, list):
            log.exception("histogram_percentiles should be a list - ignoring")
            self.data.pop('histogram_percentiles')
            return

        result = []
        for val in percentiles_config:
            try:
                if isinstance(val, str):
                    val = val.strip()
                floatval = float(val)
                if floatval <= 0 or floatval >= 1:
                    raise ValueError

                if str(floatval)[::-1].find('.') > 2:
                    # round to two decimal places
                    floatval = float(
                        decimal.Decimal(floatval).quantize(
                            decimal.Decimal('.01'), rounding=decimal.ROUND_DOWN)
                    )
                result.append(floatval)
            except ValueError:
                log.warning("Bad histogram percentile value %s, must be float in ]0;1[, skipping", val)
            except Exception:
                log.exception("Error when parsing histogram percentiles, skipping")
                return None

        self.data['histogram_percentiles'] = result

    def add_provider(self, source, provider):
        """ Adds ConfigProvider for check configurations """
        if not isinstance(provider, ConfigProvider):
            raise ValueError("expected a configuration provider")

        self._providers[source] = provider

    def collect_check_configs(self):
        """ Iterates providers collecting configurations """
        for source, provider in self._providers.items():
            checksconfigs = provider.collect()
            for check, configs in checksconfigs.items():
                current_configs = self._check_configs[source].get(check, [])
                for config in configs:
                    if config in current_configs:
                        # skip existing ones in case we re-call this
                        continue

                    current_configs.append(config)

                self._check_configs[source][check] = current_configs

    def get_check_configs(self):
        return self._check_configs
