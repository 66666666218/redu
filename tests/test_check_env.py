"""scripts/check_env.py 解析语义单测(对齐 python-dotenv 加载)。"""
from scripts.check_env import parse_dotenv


def test_parses_export_and_case_insensitive() -> None:
    d = parse_dotenv("export DATABASE_URL=mysql://x\nJWT_SECRET=abc\n")
    assert d["DATABASE_URL"] == "mysql://x"      # export 前缀
    assert d["JWT_SECRET"] == "abc"              # 键统一大写(大小写不敏感)


def test_strips_inline_comment_and_empty_value() -> None:
    d = parse_dotenv("ADMIN_EMAIL=            # 用该邮箱注册即成为管理员\nJWT_SECRET=   # comment\n")
    assert d["ADMIN_EMAIL"] == ""                # 空+注释 → 视为空
    assert d["JWT_SECRET"] == ""


def test_keeps_hash_inside_quotes() -> None:
    d = parse_dotenv('SECRET="#keep#"\n')
    assert d["SECRET"] == "#keep#"               # 引号内的 # 保留


def test_skips_comment_and_blank_lines() -> None:
    d = parse_dotenv("# 注释\n\nJWT_SECRET=abc\n")
    assert list(d) == ["JWT_SECRET"]


def test_finds_no_key_on_malformed_line() -> None:
    assert parse_dotenv("no equals here\n") == {}
