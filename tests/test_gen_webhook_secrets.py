"""Tests for the Azure webhook secret generator."""

from __future__ import annotations

from scripts import gen_webhook_secrets


def test_generator_uses_expected_entropy_and_prints_azure_command(monkeypatch, capsys) -> None:
    lengths: list[int] = []

    def token_urlsafe(length: int) -> str:
        lengths.append(length)
        return "path-value" if length == 24 else "secret-value"

    monkeypatch.setattr(gen_webhook_secrets.secrets, "token_urlsafe", token_urlsafe)

    assert gen_webhook_secrets.generate_webhook_settings() == ("path-value", "secret-value")
    assert lengths == [24, 32]

    gen_webhook_secrets.main()
    command = capsys.readouterr().out.strip()
    assert command == (
        "az webapp config appsettings set --name <APP_NAME> "
        "--resource-group <RESOURCE_GROUP> --settings "
        "TELEGRAM_WEBHOOK_PATH=path-value TELEGRAM_WEBHOOK_SECRET=secret-value"
    )


def test_generator_outputs_urlsafe_values_with_expected_lengths() -> None:
    path, secret = gen_webhook_secrets.generate_webhook_settings()
    assert len(path) == 32
    assert len(secret) == 43
    assert path.replace("-", "").replace("_", "").isalnum()
    assert secret.replace("-", "").replace("_", "").isalnum()
