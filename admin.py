"""账号与 API token 管理 CLI（本地管理员使用，不经 HTTP 暴露）。

示例：
  python admin.py create-user --username alice --role user --daily-quota 20
  python admin.py issue-token --username alice --name laptop
  python admin.py list-users
  python admin.py audit --limit 50
"""
import argparse
import json

import auth


def _print(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description="Deep Research 账号/token 管理")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-user", help="创建用户")
    p.add_argument("--username", required=True)
    p.add_argument("--role", choices=("user", "admin"), default="user")
    p.add_argument("--daily-quota", type=int, default=None, help="0 表示不限")

    p = sub.add_parser("issue-token", help="签发 API token（明文只显示一次）")
    p.add_argument("--username", required=True)
    p.add_argument("--name", default="default")

    p = sub.add_parser("list-users", help="列出用户")

    p = sub.add_parser("list-tokens", help="列出 token（不含哈希）")
    p.add_argument("--username", default=None)

    p = sub.add_parser("disable-user", help="禁用用户及其 token")
    p.add_argument("--username", required=True)

    p = sub.add_parser("disable-token", help="禁用单个 token")
    p.add_argument("--id", type=int, required=True)

    p = sub.add_parser("audit", help="查看最近审计事件")
    p.add_argument("--limit", type=int, default=50)

    p = sub.add_parser("stats", help="账号/审计计数")

    args = parser.parse_args()
    try:
        if args.command == "create-user":
            user = auth.create_user(
                args.username, role=args.role, daily_quota=args.daily_quota
            )
            auth.audit("cli", "create_user", detail=user["username"])
            _print(user)
        elif args.command == "issue-token":
            token, token_id = auth.issue_token(args.username, name=args.name)
            auth.audit(
                "cli", "issue_token", detail=f"{args.username}:{token_id}"
            )
            _print({
                "token_id": token_id,
                "token": token,
                "warning": "明文 token 只显示一次，请立即保存到密码管理器",
            })
        elif args.command == "list-users":
            _print(auth.list_users())
        elif args.command == "list-tokens":
            _print(auth.list_tokens(args.username))
        elif args.command == "disable-user":
            updated = auth.disable_user(args.username)
            auth.audit("cli", "disable_user", detail=f"{args.username}:{updated}")
            _print({"updated": updated})
        elif args.command == "disable-token":
            updated = auth.disable_token(args.id)
            auth.audit("cli", "disable_token", detail=f"{args.id}:{updated}")
            _print({"updated": updated})
        elif args.command == "audit":
            _print(auth.list_audit(args.limit))
        elif args.command == "stats":
            _print(auth.stats())
    except ValueError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
