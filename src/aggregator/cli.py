"""python -m aggregator.cli create-admin EMAIL   (asks for the password)"""

import argparse
import asyncio
import getpass
import os

from sqlalchemy.dialects.postgresql import insert

from aggregator.auth import hash_password
from aggregator.db import get_sessionmaker
from aggregator.models import User


async def create_admin(email: str, password: str) -> None:
    values = {"password_hash": hash_password(password), "is_admin": True}
    stmt = insert(User).values(email=email.lower(), **values)
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(stmt.on_conflict_do_update(index_elements=["email"], set_=values))


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(required=True)
    command = commands.add_parser("create-admin", help="create an admin or reset the password")
    command.add_argument("email")
    args = parser.parse_args()
    password = os.environ.get("ADMIN_PASSWORD") or getpass.getpass("Password: ")
    if len(password) < 8:
        parser.error("password is too short, 8 characters minimum")
    asyncio.run(create_admin(args.email, password))
    print(f"admin {args.email} is ready")


if __name__ == "__main__":
    main()
