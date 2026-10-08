import pytest

import server


@pytest.fixture
def patched_nornir(monkeypatch):
    """Install a FakeNornir and record the arguments _get_nornir was called with."""

    calls = []

    def install(nr):
        def fake_get_nornir(filter_criteria=None, num_workers=None):
            calls.append({"filter_criteria": filter_criteria, "num_workers": num_workers})
            return nr

        monkeypatch.setattr(server, "_get_nornir", fake_get_nornir)
        return nr

    install.calls = calls
    return install
