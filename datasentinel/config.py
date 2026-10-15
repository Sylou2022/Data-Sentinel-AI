"""Configuration centralisée, lue depuis l'environnement ou le fichier .env (aucun secret dans le code)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Les variables déjà définies dans l'environnement restent prioritaires sur le fichier .env.
load_dotenv(PROJECT_ROOT / ".env", override=False)


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)



@dataclass(frozen=True)
class SqlServerSettings:
    server: str = field(default_factory=lambda: _env("SQLSERVER_SERVER", r"localhost\SQLEXPRESS"))
    database: str = field(default_factory=lambda: _env("SQLSERVER_DATABASE", "Data_Sentinel_AI"))
    driver: str = field(default_factory=lambda: _env("SQLSERVER_DRIVER", "ODBC Driver 18 for SQL Server"))

    tenant_id: str = field(default_factory=lambda: _env("SQLSERVER_TENANT_ID"))
    client_id: str = field(default_factory=lambda: _env("SQLSERVER_CLIENT_ID"))
    client_secret: str = field(default_factory=lambda: _env("SQLSERVER_CLIENT_SECRET"), repr=False)

    trusted_connection: bool = field(default_factory=lambda: _env("SQLSERVER_TRUSTED_CONNECTION", "yes").lower() in ("yes", "true", "1"))
    user: str = field(default_factory=lambda: _env("SQLSERVER_USER"))
    password: str = field(default_factory=lambda: _env("SQLSERVER_PASSWORD"), repr=False)
    trust_server_certificate: bool = field(default_factory=lambda: _env("SQLSERVER_TRUST_SERVER_CERTIFICATE", "no").lower() in ("yes", "true", "1"))
    mapping_file: str = field(default_factory=lambda: _env("SQLSERVER_MAPPING_FILE"))

    @property
    def azure(self) -> bool:
        """Authentification Microsoft Entra (principal de service) dès que ses trois variables sont renseignées."""
        return bool(self.tenant_id and self.client_id and self.client_secret)

    @property
    def auth_label(self) -> str:
        if self.azure:
            return "principal de service Microsoft Entra"
        return "authentification Windows" if self.trusted_connection else f"utilisateur {self.user}"

    def connection_string(self) -> str:
        """Sans les identifiants (mot de passe ou jeton), ajoutés au moment de la connexion."""
        parts = [
            f"DRIVER={{{self.driver}}}",
            f"SERVER={self.server}",
            f"DATABASE={self.database}",
            "ApplicationIntent=ReadOnly",
            "Encrypt=yes",
            f"TrustServerCertificate={'yes' if self.trust_server_certificate else 'no'}",
        ]
        if not self.azure:
            parts.append("Trusted_Connection=yes" if self.trusted_connection else f"UID={self.user}")
        return ";".join(parts) + ";"



@dataclass(frozen=True)
class Settings:
    home: Path = field(default_factory=lambda: Path(os.getenv("DATASENTINEL_HOME", PROJECT_ROOT / "var")))
    # "none" = rédaction déterministe locale ; "anthropic" = opt-in explicite vers Claude.
    llm_provider: str = field(default_factory=lambda: os.getenv("DATASENTINEL_LLM_PROVIDER", "none").lower())
    llm_model: str = field(default_factory=lambda: os.getenv("DATASENTINEL_LLM_MODEL", "claude-opus-5-5"))
    sqlserver: SqlServerSettings = field(default_factory=SqlServerSettings)

    # Seuils de détection
    detection_window_days: int = 21
    min_relative_deviation: float = 0.25
    robust_z_threshold: float = 4.0
    price_outlier_ratio: float = 5.0

    @property
    def labs_dir(self) -> Path:
        return self.home / "labs"

    @property
    def sources_dir(self) -> Path:
        return self.home / "sources"

    @property
    def audit_db(self) -> Path:
        return self.home / "audit.db"

    @property
    def checkpoint_db(self) -> Path:
        return self.home / "checkpoints.db"

    def ensure_dirs(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.labs_dir.mkdir(parents=True, exist_ok=True)
        self.sources_dir.mkdir(parents=True, exist_ok=True)


def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s
