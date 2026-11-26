#!/usr/bin/env bash
# 校验项目内的技能副本是否与上游固定 commit 逐字节一致
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"   # skills-deps/
PROJ="${PROJECT_ROOT:-$HOME/short_v/有谁共鸣2}"
MAP="$ROOT/meta/skills.map.json"
TMP="$(mktemp -d)"
fail=0

python3 - "$MAP" > "$TMP/list.txt" <<'PY'
import json, sys
m = json.load(open(sys.argv[1], encoding="utf-8"))
for name, rec in m["skills"].items():
    base = rec["repo"].replace("https://github.com/", "https://raw.githubusercontent.com/").rstrip("/")
    for rel, want in rec["files"].items():
        print(name, rec["commit"], rel, want, f'{base}/{rec["commit"]}/{rec["path"]}/{rel}')
PY

while read -r name commit rel want url; do
  tmpfile="$TMP/${name}__${rel//\//_}"
  if ! curl -sS --retry 4 --retry-delay 2 --retry-all-errors --max-time 90 --connect-timeout 20 -o "$tmpfile" "$url"; then
    echo "❌ 下载失败 $name/$rel"; fail=1; continue
  fi
  up=$(sha1sum "$tmpfile" | cut -d' ' -f1)
  local_f="$PROJ/.agents/skills-upstream/$name/$rel"
  if [ ! -f "$local_f" ]; then echo "❌ 本地缺文件 $name/$rel"; fail=1; continue; fi
  loc=$(sha1sum "$local_f" | cut -d' ' -f1)
  if [ "$up" = "$loc" ] && [ "$loc" = "$want" ]; then
    echo "✅ $name/$rel  $loc"
  else
    echo "❌ $name/$rel  上游=$up 本地=$loc 清单=$want"; fail=1
  fi
done < "$TMP/list.txt"

rm -rf "$TMP"
exit $fail
