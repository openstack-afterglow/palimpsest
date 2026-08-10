from __future__ import annotations

import openstack

from palimpsest_hub.config import get_settings


def get_image(conn: openstack.connection.Connection, image_id: str):
    return conn.image.get_image(image_id)


def get_admin_connection_for_project(project_id: str) -> openstack.connection.Connection:
    settings = get_settings()
    return openstack.connect(
        load_envvars=False,
        load_yaml_config=False,
        auth_url=settings.os_auth_url,
        auth_type="password",
        username=settings.os_username,
        password=settings.os_password.get_secret_value(),
        project_id=project_id,
        user_domain_name=settings.os_user_domain_name,
        project_domain_name=settings.os_project_domain_name,
        region_name=settings.os_region_name,
        interface=settings.os_interface,
        api_timeout=30,
        verify=settings.ssl_verify,
    )
