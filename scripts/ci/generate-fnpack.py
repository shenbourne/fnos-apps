#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate-fnpack.py — 为本仓库（conversun/fnos-apps 的 fork）生成 FnDepot 外部源 V2 索引 fnpack.json

本脚本随仓库发布，manifest、图标、README 均来自本仓库（相对 URL），
.fpk 安装包引用 conversun/fnos-apps 的 GitHub Release 资产（含 size / sha256 digest）。

用法（CI 或本地）：
    python3 scripts/ci/generate-fnpack.py            # 写出 fnpack.json
    python3 scripts/ci/generate-fnpack.py --check    # 与现有 fnpack.json 对比（CI 校验）

认证：设置 GH_TOKEN / GITHUB_TOKEN 可走认证配额（CI 中即 GITHUB_TOKEN，可读取公开仓库）。
"""
import json
import os
import re
import sys
import time
import urllib.request

UPSTREAM_REPO = "conversun/fnos-apps"   # .fpk 构建与发布所在的上游仓库
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OUTPUT = os.path.join(REPO_ROOT, "fnpack.json")
MAX_VERSIONS_PER_APP = 3          # FnDepot 允许单应用最多 100 个版本，保留最近 3 个即可回滚

# meta.env 分类 → FnDepot V2 九个固定分类（只能从中选择）
CATEGORY_MAP = {
    "ai": ["AI赋能"],
    "media": ["影音娱乐"],
    "download": ["生活服务"],
    "system": ["系统工具"],
    "network": ["系统工具"],
    "automation": ["智能智控"],
    "browser": ["影音娱乐"],
    "content": ["生活服务"],
    "store": ["系统工具"],
}
DEFAULT_CATEGORY = ["系统工具"]

# FnDepot 要求版本号可比较、不能用 "latest" 或纯日期文字
VERSION_RE = re.compile(r"^\d+(?:\.\d+)*(?:[-._][0-9A-Za-z._-]+)?$")


def parse_manifest(path):
    data = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line.strip())
            if m:
                data[m.group(1)] = m.group(2).strip()
    return data


def parse_meta_env(path):
    data = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = re.match(r'^([A-Z_][A-Z0-9_]*)=(.*)$', line.strip())
            if m:
                data[m.group(1)] = m.group(2).strip().strip('"')
    return data


def gh_api(url):
    """调用 GitHub REST API 并自动翻页，返回合并后的列表。"""
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    items, page = [], 1
    while True:
        sep = "&" if "?" in url else "?"
        purl = f"{url}{sep}per_page=100&page={page}"
        req = urllib.request.Request(purl, headers={
            "User-Agent": "fnpack-generator",
            "Accept": "application/vnd.github+json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        })
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    batch = json.loads(resp.read().decode("utf-8"))
                break
            except Exception as e:
                if attempt == 4:
                    raise
                print(f"  [retry] {purl}: {e}", file=sys.stderr)
                time.sleep(5 * (attempt + 1))
        if not isinstance(batch, list) or not batch:
            break
        items.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return items


def list_releases():
    return gh_api(f"https://api.github.com/repos/{UPSTREAM_REPO}/releases")


def norm_version(tag, slug):
    """从 release tag 提取干净的版本号：'plex/v1.42.2-r1' -> '1.42.2-r1'"""
    v = tag[len(slug):].lstrip("/")
    v = re.sub(r"^v+", "", v)
    return v


def arch_of(asset_name):
    low = asset_name.lower()
    if low.endswith("_x86.fpk"):
        return "x86"
    if low.endswith("_arm.fpk"):
        return "arm"
    if low.endswith(".fpk"):
        return "all"
    return None


def build_app(slug, manifest, meta, releases):
    appname = manifest.get("appname", slug)
    packages_by_version = {}
    for rel in releases:
        tag = rel["tag_name"]
        if not tag.startswith(slug + "/"):
            continue
        version = norm_version(tag, slug)
        if not VERSION_RE.match(version) or "latest" in version.lower():
            print(f"  [skip] {tag}: 版本号不合规", file=sys.stderr)
            continue
        pkgs = {}
        for a in rel.get("assets", []):
            arch = arch_of(a["name"])
            if not arch:
                continue
            digest = a.get("digest") or ""
            sha256 = digest.split(":", 1)[1] if digest.startswith("sha256:") else ""
            entry = {
                "download_url": f"https://github.com/{UPSTREAM_REPO}/releases/download/{tag}/{a['name']}",
                "size": a["size"],
            }
            if sha256:
                entry["sha256"] = sha256
            pkgs[arch] = entry
        if not pkgs:
            continue
        published = rel.get("published_at") or ""
        packages_by_version[version] = {
            "updated_at": published,
            "packages": pkgs,
        }

    if not packages_by_version:
        return None

    # 只保留最近 MAX_VERSIONS_PER_APP 个版本（按发布时间排序）
    sorted_versions = sorted(
        packages_by_version.items(),
        key=lambda kv: kv[1]["updated_at"],
        reverse=True,
    )[:MAX_VERSIONS_PER_APP]

    appdir = os.path.join(REPO_ROOT, "apps", slug, "fnos")
    is_docker = os.path.isfile(os.path.join(appdir, "docker", "docker-compose.yaml"))
    categories = CATEGORY_MAP.get((meta.get("CATEGORY") or "").lower(), DEFAULT_CATEGORY)

    icon = "ICON_256.PNG" if os.path.isfile(os.path.join(appdir, "ICON_256.PNG")) else "ICON.PNG"
    app = {
        "display_name": manifest.get("display_name", slug),
        "desc": manifest.get("desc", ""),
        "platform": ["x86", "arm"],
        "categories": categories,
        # 相对 URL：相对本仓库根目录的 fnpack.json 解析，图标随仓库分发
        "icon_url": f"./apps/{slug}/fnos/{icon}",
        "maintainer": manifest.get("maintainer", ""),
        "maintainer_url": manifest.get("maintainer_url", ""),
        "distributor": "conversun",
        "distributor_url": f"https://github.com/{UPSTREAM_REPO}",
        "run_as": "package",
        "install_type": "",
        "is_docker": is_docker,
        "service_port": str(manifest.get("service_port", "") or ""),
        "releases": {v: data for v, data in sorted_versions},
    }
    if os.path.isfile(os.path.join(REPO_ROOT, "apps", slug, "README.md")):
        app["readme_url"] = f"./apps/{slug}/README.md"
    return appname, app


def main():
    releases = list_releases()
    print(f"Fetched {len(releases)} releases from {UPSTREAM_REPO}", file=sys.stderr)

    apps = {}
    scripts_apps = os.path.join(REPO_ROOT, "scripts", "apps")
    for slug in sorted(os.listdir(scripts_apps)):
        meta_path = os.path.join(scripts_apps, slug, "meta.env")
        manifest_path = os.path.join(REPO_ROOT, "apps", slug, "fnos", "manifest")
        if not (os.path.isfile(meta_path) and os.path.isfile(manifest_path)):
            continue
        manifest = parse_manifest(manifest_path)
        meta = parse_meta_env(meta_path)
        app_releases = [r for r in releases if r["tag_name"].startswith(slug + "/")]
        if not app_releases:
            print(f"  [warn] {slug}: 无 release，跳过", file=sys.stderr)
            continue
        result = build_app(slug, manifest, meta, app_releases)
        if not result:
            print(f"  [warn] {slug}: 无合规版本，跳过", file=sys.stderr)
            continue
        appname, app = result
        if appname in apps:
            print(f"  [error] appname 冲突: {appname} ({slug})", file=sys.stderr)
            sys.exit(1)
        apps[appname] = app
        print(f"  ✓ {slug} -> {appname} ({len(app['releases'])} 版本)", file=sys.stderr)

    if not apps:
        print("[error] 没有任何可用应用", file=sys.stderr)
        sys.exit(1)

    doc = {
        "schema_version": "2",
        "source_info": {
            "name": "fnOS Apps",
            "author": "shenbourne",
            "homepage": f"https://github.com/{UPSTREAM_REPO}",
            "description": "面向飞牛 fnOS 的第三方应用打包仓库 conversun/fnos-apps 的 FnDepot 外部源索引。",
        },
        "apps": apps,
    }

    text = json.dumps(doc, ensure_ascii=False, indent=2)
    if len(text.encode("utf-8")) > 2 * 1024 * 1024:
        print("[error] fnpack.json 超过 FnDepot 2MB 上限，请改用 details_url 拆分模式", file=sys.stderr)
        sys.exit(1)

    if "--check" in sys.argv:
        with open(OUTPUT, encoding="utf-8") as f:
            if f.read() != text + "\n":
                print("[error] fnpack.json 不是最新，请先运行生成脚本", file=sys.stderr)
                sys.exit(1)
        print("fnpack.json 已是最新")
        return

    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print(f"Generated fnpack.json: {len(apps)} apps, {len(text.encode('utf-8'))} bytes", file=sys.stderr)


if __name__ == "__main__":
    main()
