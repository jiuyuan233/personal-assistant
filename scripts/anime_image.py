"""早安二次元图：复用公开 SFW 图库 nekos.best，优先取「图源标注为 Pixiv」的那张。

选型是实测出来的，不是拍脑袋（三条路都试过）：
- **直接爬 P 站**：`www.pixiv.net` 与 `i.pximg.net` 在国内出口实测 curl code 000（不可达），
  且要登录态、有风控。用户在国内，发过去的链接他也打不开。
- **lolicon setu v2**（返回 Pixiv 直链，看起来最贴合「来源于P站」）：**弃用**。
  实测 `r18=0` 并不安全 —— 40 张里只有 10 张能过黑名单，返回里出现带 `R-18` 标签却
  `r18:false` 的条目，以及 `小学生`+`全裸`、`下着`、`巨乳`、`スク水` 等标签。
  按主题标签（風景/猫/空/夜景/桜）约束也没用，这些标签是和色情内容**并存**而不是互斥。
  每天自动发这种图不可接受，所以整条路放弃。
- **nekos.best**（采用）：公开 SFW 图库，实测 48 张里 **34 张 source_url 指向 pixiv.net**，
  带 artist_name / artist_href / source_url，能溯源到 P 站作品页；图片托管在它自己的 CDN 上，
  国内可直连。取 5 张候选、优先挑 Pixiv 图源，命中率 1-(1-0.71)^5 ≈ 99.8%。

⚠️ 飞书自定义机器人 webhook **不能用 URL 内嵌图片**：卡片 img 元素只认 image_key，
而 image_key 必须用企业自建应用的 tenant_access_token 调 /im/v1/images 上传才拿得到。
所以两条路径：
  配了 FS_APP_ID + FS_APP_SECRET → 下载图片、上传换 image_key、发真图；
  没配 → 发「点开看图」按钮直链（webhook 卡片按钮是本项目已验证可用的元素）。
两条路径都不阻断推送：取图失败就当没有图，消息照发。
"""
from __future__ import annotations

import json
import os
import random
from pathlib import Path

import requests

from utils import env, log, safe_text

API = "https://nekos.best/api/v2/{endpoint}"
# 只用静态图分类。endpoints 里还有一堆 gif 动作分类（poke/smile/…），那不是「二次元插画」。
ENDPOINTS = ("neko", "kitsune", "waifu")
CANDIDATES = 5
IMAGE_PATH = Path(os.environ.get("PUSH_IMAGE_PATH", "/tmp/push-image.json"))
TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
UPLOAD_URL = "https://open.feishu.cn/open-apis/im/v1/images"
# 飞书上传图片上限 10MB，留一点余量；超了就退回链接模式，别把推送整个搞失败。
MAX_BYTES = 8 * 1024 * 1024
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")


def is_pixiv(item: dict) -> bool:
    return "pixiv.net" in (item.get("source_url") or "")


def pick(results: list[dict]) -> dict | None:
    """优先 Pixiv 图源；一张都没有就退回任意一张（仍是 SFW 二次元图，但如实标注来源）。"""
    clean = [r for r in results if r.get("url")]
    if not clean:
        return None
    for r in clean:
        if is_pixiv(r):
            return r
    return clean[0]


def fetch() -> dict | None:
    """取一张图。任何异常都返回 None —— 没有图也要照常推送。"""
    order = list(ENDPOINTS)
    random.shuffle(order)
    for ep in order:
        try:
            r = requests.get(API.format(endpoint=ep), params={"amount": CANDIDATES},
                             headers={"User-Agent": UA}, timeout=25)
            r.raise_for_status()
            results = (r.json() or {}).get("results") or []
        except Exception as e:
            log(f"[image] {ep} 取图失败：{type(e).__name__}", level="WARN")
            continue
        item = pick(results)
        if not item:
            continue
        out = {
            "endpoint": ep,
            "url": item["url"],
            "artist": safe_text(item.get("artist_name"), 60).strip() or "佚名",
            "source_url": safe_text(item.get("source_url"), 300).strip(),
            "artist_href": safe_text(item.get("artist_href"), 300).strip(),
            "pixiv": is_pixiv(item),
        }
        log(f"[image] 选中 {ep} · 画师 {out['artist']} · "
            f"图源 {'P站' if out['pixiv'] else (out['source_url'] or '未标注')}")
        return out
    log("[image] 三个分类都没取到图，本次不带图", level="WARN")
    return None


def _tenant_token(app_id: str, app_secret: str) -> str:
    r = requests.post(TOKEN_URL, json={"app_id": app_id, "app_secret": app_secret},
                      timeout=25)
    j = r.json()
    if j.get("code") != 0:
        # 只打 code 和 msg，绝不把 token 或 secret 写进日志（Actions 日志是公开的）
        raise RuntimeError(f"tenant_access_token code={j.get('code')} msg={j.get('msg')}")
    return j.get("tenant_access_token") or ""


def upload(image_url: str) -> str:
    """下载图片并上传到飞书换 image_key。拿不到就返回空串，让调用方退回链接模式。

    只在配了 FS_APP_ID + FS_APP_SECRET 时才会走到这里；默认配置根本不下载图片，
    所以链接模式不会因为大图下载慢而拖住整个推送。
    """
    app_id = env("FS_APP_ID")
    app_secret = env("FS_APP_SECRET")
    if not app_id or not app_secret:
        return ""
    try:
        token = _tenant_token(app_id, app_secret)
        if not token:
            return ""
        img = requests.get(image_url, headers={"User-Agent": UA}, timeout=60)
        img.raise_for_status()
        ctype = img.headers.get("content-type") or ""
        if not ctype.startswith("image/"):
            log(f"[image] 下载到的不是图片（content-type={ctype}），退回链接", level="WARN")
            return ""
        if len(img.content) > MAX_BYTES:
            log(f"[image] 图片 {len(img.content)} 字节超过飞书上限，退回链接", level="WARN")
            return ""
        r = requests.post(UPLOAD_URL,
                          headers={"Authorization": f"Bearer {token}"},
                          data={"image_type": "message"},
                          files={"image": ("anime.png", img.content, ctype)},
                          timeout=60)
        j = r.json()
        if j.get("code") != 0:
            log(f"[image] 飞书上传失败 code={j.get('code')} msg={j.get('msg')}，退回链接",
                level="WARN")
            return ""
        key = ((j.get("data") or {}).get("image_key")) or ""
        log(f"[image] 已换取 image_key（{len(key)} 字符），本次发内嵌真图")
        return key
    except Exception as e:
        log(f"[image] 上传链路失败：{type(e).__name__}，退回链接", level="WARN")
        return ""


def run() -> dict | None:
    """取图 →（可选）上传 → 落盘给 notify_feishu.js 读。返回 None 表示本次没有图。"""
    item = fetch()
    if not item:
        IMAGE_PATH.unlink(missing_ok=True)
        return None
    item["image_key"] = upload(item["url"])
    IMAGE_PATH.write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")
    return item


if __name__ == "__main__":
    run()