"""Operator commands: python -m nota.admin set-plan <user_id> <free|pro>"""
import sys

from nota import db, jobs


def main(argv):
    if len(argv) != 3 or argv[0] != "set-plan" or argv[2] not in ("free", "pro"):
        sys.exit("usage: python -m nota.admin set-plan <user_id> <free|pro>")
    _, user_id, plan = argv
    with db.tx() as conn:
        if not conn.execute("UPDATE users SET plan = %s WHERE id = %s", (plan, user_id)).rowcount:
            sys.exit(f"user {user_id} not found")
        jobs.wake_workers(conn)  # recordings paused by quota may now fit
    print(f"{user_id} -> {plan}")


if __name__ == "__main__":
    main(sys.argv[1:])
