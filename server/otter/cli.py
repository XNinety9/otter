"""Account and API token management.

    python -m otter.cli create-user admin
    python -m otter.cli create-user alice --role viewer     # sees everything, changes nothing
    python -m otter.cli set-role alice admin
    python -m otter.cli create-token ci --user admin
    python -m otter.cli list-tokens
    python -m otter.cli revoke-token ci

With Docker: docker compose exec otter python -m otter.cli create-user admin
"""

import argparse
import getpass
import sys

from sqlalchemy import delete, select

from . import auth, config
from .db import SessionLocal
from .migrate import upgrade_database
from .models import ApiToken, AuthSession, User


def read_password(args) -> str:
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass("Password: ")
        if getpass.getpass("Repeat: ") != password:
            sys.exit("Passwords don't match.")
    if len(password) < 8:
        sys.exit("Use at least 8 characters.")
    return password


def get_user(session, username: str) -> User:
    user = session.scalar(select(User).where(User.username == username))
    if user is None:
        sys.exit(f"No user named {username!r}.")
    return user


ROLES = ("admin", "viewer")


def create_user(args) -> None:
    with SessionLocal() as session:
        if session.scalar(select(User).where(User.username == args.username)):
            sys.exit(f"User {args.username!r} already exists.")
        session.add(User(username=args.username, password_hash=auth.hash_password(read_password(args)), role=args.role))
        session.commit()
    print(f"User {args.username!r} created ({args.role}).")


def set_role(args) -> None:
    with SessionLocal() as session:
        get_user(session, args.username).role = args.role
        session.commit()
    print(f"{args.username!r} is now {args.role}.")


def set_password(args) -> None:
    with SessionLocal() as session:
        user = get_user(session, args.username)
        user.password_hash = auth.hash_password(read_password(args))
        session.execute(delete(AuthSession).where(AuthSession.user_id == user.id))  # log out everywhere
        session.commit()
    print(f"Password of {args.username!r} changed; existing sessions were closed.")


def list_users(args) -> None:
    with SessionLocal() as session:
        for user in session.scalars(select(User).order_by(User.username)):
            print(f"{user.username}\t{user.role}\tcreated {user.created_at:%Y-%m-%d}")


def delete_user(args) -> None:
    with SessionLocal() as session:
        session.delete(get_user(session, args.username))
        session.commit()
    print(f"User {args.username!r} deleted, with their sessions and tokens.")


def create_token(args) -> None:
    with SessionLocal() as session:
        if session.scalar(select(ApiToken).where(ApiToken.name == args.name)):
            sys.exit(f"A token named {args.name!r} already exists.")
        token = auth.new_api_token()
        session.add(ApiToken(name=args.name, token_hash=auth.token_hash(token), user=get_user(session, args.user)))
        session.commit()
    print(f"Token {args.name!r} created. Copy it now, it won't be shown again:\n{token}")


def list_tokens(args) -> None:
    with SessionLocal() as session:
        for token in session.scalars(select(ApiToken).order_by(ApiToken.name)):
            used = f"{token.last_used_at:%Y-%m-%d %H:%M} UTC" if token.last_used_at else "never"
            print(f"{token.name}\tuser {token.user.username}\tlast used {used}")


def revoke_token(args) -> None:
    with SessionLocal() as session:
        token = session.scalar(select(ApiToken).where(ApiToken.name == args.name))
        if token is None:
            sys.exit(f"No token named {args.name!r}.")
        session.delete(token)
        session.commit()
    print(f"Token {args.name!r} revoked.")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="otter.cli", description="Manage Otter accounts and API tokens.")
    commands = parser.add_subparsers(required=True)

    for name, func, help_ in [
        ("create-user", create_user, "create an account"),
        ("set-password", set_password, "change a password (closes the user's sessions)"),
    ]:
        cmd = commands.add_parser(name, help=help_)
        cmd.add_argument("username")
        cmd.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
        if func is create_user:
            cmd.add_argument("--role", choices=ROLES, default="admin", help="viewer: sees everything, changes nothing")
        cmd.set_defaults(func=func)

    cmd = commands.add_parser("set-role", help="make an account admin or viewer")
    cmd.add_argument("username")
    cmd.add_argument("role", choices=ROLES)
    cmd.set_defaults(func=set_role)

    cmd = commands.add_parser("list-users", help="list accounts")
    cmd.set_defaults(func=list_users)
    cmd = commands.add_parser("delete-user", help="delete an account, its sessions and tokens")
    cmd.add_argument("username")
    cmd.set_defaults(func=delete_user)

    cmd = commands.add_parser("create-token", help="create an API token (printed once)")
    cmd.add_argument("name")
    cmd.add_argument("--user", required=True, help="account the token acts as")
    cmd.set_defaults(func=create_token)
    cmd = commands.add_parser("list-tokens", help="list API tokens")
    cmd.set_defaults(func=list_tokens)
    cmd = commands.add_parser("revoke-token", help="delete an API token")
    cmd.add_argument("name")
    cmd.set_defaults(func=revoke_token)

    args = parser.parse_args(argv)
    config.FIRMWARE_DIR.mkdir(parents=True, exist_ok=True)
    upgrade_database()
    args.func(args)


if __name__ == "__main__":
    main()
