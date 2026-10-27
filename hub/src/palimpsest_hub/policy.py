"""Service-local oslo.policy decisions; identity and resource boundaries live in auth/registry."""

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
# Capability names are presentation labels, never synthesized Keystone role claims.
CAPABILITY_ACTIONS = {
    "palimpsest-inventory_reader": "packages:inventory",
    "palimpsest-download_user": "packages:read",
    "palimpsest-publish_editor": "packages:write",
    "palimpsest-keys_editor": "keys:issue",
    "palimpsest-keys_admin": "keys:revoke",
}
_READ = "role:admin or role:palimpsest_admin or role:palimpsest_editor or role:palimpsest_user"
_WRITE = "role:admin or role:palimpsest_admin or role:palimpsest_editor"
_INVENTORY = (
    _READ
    + " or role:palimpsest_reader or role:palimpsest-inventory_reader or role:palimpsest-download_user or role:palimpsest-publish_editor or role:palimpsest-keys_editor or role:palimpsest-keys_admin"
)
_CHECKS = {
    "packages:inventory": _INVENTORY,
    "packages:read": _READ + " or role:palimpsest-download_user",
    "packages:write": _WRITE + " or role:palimpsest-publish_editor",
    "cache:read": _READ + " or role:palimpsest-download_user",
    "cache:write": _WRITE + " or role:palimpsest-publish_editor",
    "keys:issue": _WRITE + " or role:palimpsest-keys_editor",
    "keys:list": _WRITE + " or role:palimpsest-keys_editor or role:palimpsest-keys_admin",
    "keys:revoke": "role:admin or role:palimpsest_admin or role:palimpsest-keys_admin",
    "system:manage": "role:admin",
    "packages:delete": "role:admin",
}
ACTIONS = tuple(sorted(_CHECKS))
KEY_ACTIONS = frozenset({"packages:inventory", "packages:read", "packages:write", "cache:read", "cache:write"})
SYSTEM_ACTIONS = frozenset({"system:manage"})


def list_rules():
    return [
        policy.RuleDefault(
            name,
            check if name in SYSTEM_ACTIONS else "personal_project_owner:True or " + check,
            description=("System-only " if name in SYSTEM_ACTIONS else "Project-local ")
            + name
            + "; native ownership and credential limits always apply.",
            scope_types=["project"],
        )
        for name, check in _CHECKS.items()
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


def allowed_actions(
    roles, project_id: str, user_id: str = "", *, system_admin: bool = False, personal_project_owner: bool = False
) -> frozenset[str]:
    """Evaluate exact trusted global roles; no service grade-to-leaf expansion."""
    if not project_id:
        return frozenset()
    actions = tuple(
        action
        for action in ACTIONS
        if (system_admin or action not in SYSTEM_ACTIONS)
        and (action != "packages:delete" or system_admin or personal_project_owner is True)
    )
    creds = {
        "roles": list(roles),
        "project_id": project_id,
        "user_id": user_id,
        "personal_project_owner": personal_project_owner is True,
    }
    return _evaluate_actions(actions, {"project_id": project_id, "user_id": user_id}, creds)


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


def capabilities_for(actions) -> frozenset[str]:
    return frozenset(label for label, action in CAPABILITY_ACTIONS.items() if action in actions)
