"""Decrypt a token produced by password_enc.py using the original string key."""

from getpass import getpass

if __package__:
    from .password_enc import decrypt_password
else:
    from password_enc import decrypt_password


def main() -> None:
    token = input("Encrypted password: ").strip()
    key = getpass("Encryption key: ")
    try:
        print(decrypt_password(token, key))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
