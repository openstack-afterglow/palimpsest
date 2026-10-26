"""Native oslo.policy enforcement for catalog system management only."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from threading import RLock, get_ident

import yaml
from fastapi import HTTPException
from oslo_config import cfg
from oslo_policy import _parser, opts, policy

from palimpsest_hub.config import get_settings

DEFAULT_POLICY_FILE = "/etc/palimpsest/policy.yaml"
_LOCK = RLock()


def list_rules():
    return [
        policy.RuleDefault(
            "system:manage",
            "role:admin",
            description="Global catalog management; fresh human system authority always applies.",
            scope_types=["project"],
        )
    ]


class _ParseErrors:
    """Observe native parser errors for this reload thread, forwarding all logging."""

    def __init__(self, logger):
        self.logger = logger
        self.owner = get_ident()
        self.failed = False

    def __getattr__(self, name):
        return getattr(self.logger, name)

    def error(self, *args, **kwargs):
        if get_ident() == self.owner:
            self.failed = True
        return self.logger.error(*args, **kwargs)

    def exception(self, *args, **kwargs):
        if get_ident() == self.owner:
            self.failed = True
        return self.logger.exception(*args, **kwargs)


class _StrictEnforcer(policy.Enforcer):
    """Validate document types and native parse diagnostics, never expressions."""

    def _record_file_rules(self, data, overwrite=False):
        document = yaml.safe_load(data)
        if not isinstance(document, dict) or any(
            not isinstance(name, str) or not name.strip() or not isinstance(check, str)
            for name, check in document.items()
        ):
            raise ValueError("Policy must map nonempty rule names to string checks")
        original = _parser.LOG
        observed = _ParseErrors(original)
        _parser.LOG = observed
        try:
            result = super()._record_file_rules(data, overwrite)
            if observed.failed:
                raise ValueError("Native policy parser rejected a check")
            return result
        finally:
            _parser.LOG = original


@lru_cache(maxsize=8)
def _enforcer(policy_file: str, policy_dirs: tuple[str, ...]):
    conf = cfg.ConfigOpts()
    opts.set_defaults(conf, policy_file=policy_file)
    conf([], project="palimpsest", default_config_files=[])
    conf.set_override("policy_dirs", list(policy_dirs), group="oslo_policy")
    engine = _StrictEnforcer(conf)
    engine.register_defaults(list_rules())
    return engine


def get_enforcer():
    settings = get_settings()
    return _enforcer(settings.oslo_policy_policy_file, tuple(settings.oslo_policy_policy_dirs))


def global_admin_actions(user_id: str) -> frozenset[str]:
    """Only global management, after independent fresh human-system validation.

    No project identity or target is fabricated for genuinely global reads.
    Project-targeted YAML therefore cannot accidentally inherit a made-up scope.
    """
    return _evaluate_actions(("system:manage",), {"user_id": user_id}, {"roles": ["admin"], "user_id": user_id})


def _evaluate_actions(actions, target, creds):
    with _LOCK:
        try:
            settings = get_settings()
            # Missing default is normal; a typo in an explicit custom path is not.
            if (
                settings.oslo_policy_policy_file != DEFAULT_POLICY_FILE
                and not Path(settings.oslo_policy_policy_file).is_file()
            ):
                raise ValueError("Configured policy file is unavailable")
            engine = get_enforcer()
            engine.load_rules()
            engine.check_rules(raise_on_violation=True)
            return frozenset(action for action in actions if engine.authorize(action, target, creds))
        except Exception:
            # Never retain partially loaded rules or a cached malformed file after failure.
            _enforcer.cache_clear()
            raise HTTPException(status_code=503, detail="Palimpsest authorization policy unavailable") from None
