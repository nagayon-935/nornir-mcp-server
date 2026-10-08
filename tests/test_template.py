from types import SimpleNamespace

from nornir.core.inventory import Host

from server import _JINJA_ENV, custom_template_task


class TestCustomTemplateTask:
    def _build_task(self) -> SimpleNamespace:
        host = Host(
            name="router1",
            hostname="192.168.1.1",
            platform="cisco_ios",
            username="admin",
            password="supersecret",
            data={"role": "core"},
        )
        return SimpleNamespace(host=host)

    def test_renders_allowed_host_fields(self):
        task = self._build_task()
        template = _JINJA_ENV.from_string("hostname {{ host.name }} ! {{ host.hostname }} {{ host.data['role'] }}")

        result = custom_template_task(task, template)

        assert result.result == "hostname router1 ! 192.168.1.1 core"

    def test_does_not_expose_credentials(self):
        task = self._build_task()
        template = _JINJA_ENV.from_string("{{ host.password }}{{ host.username }}")

        result = custom_template_task(task, template)

        assert "supersecret" not in result.result
        assert "admin" not in result.result
