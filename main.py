#!/usr/bin/env python3
"""
X(Twitter) 博主发文自动转发到钉钉群
=====================================
通过 RSSHub 获取 X 博主的 RSS 订阅源，检测新推文后自动转发到钉钉群。

用法:
  python main.py              # 持续运行模式(守护进程)
  python main.py --once       # 单次运行模式(适合 cron)
  python main.py --test       # 测试钉钉连通性

配置: 编辑 config.json (参考 config.example.json)
"""

import json
import time
import hmac
import hashlib
import base64
import urllib.parse
import logging
import os
import sys
import re
import argparse
from pathlib import Path
from datetime import datetime

import feedparser
import requests

# ============================================================
# 日志配置
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("x-to-dingtalk")


# ============================================================
# 配置管理
# ============================================================
class Config:
    """从 config.json 加载配置，环境变量可覆盖部分字段"""

    def __init__(self, config_path="config.json"):
        self.config_path = config_path
        if not os.path.exists(config_path):
            logger.error(f"配置文件不存在: {config_path}")
            logger.error("请复制 config.example.json 为 config.json 并修改配置")
            sys.exit(1)

        with open(config_path, "r", encoding="utf-8") as f:
            self.data = json.load(f)

        # 环境变量覆盖 (Docker 部署时使用)
        env_webhook = os.environ.get("DINGTALK_WEBHOOK")
        env_secret = os.environ.get("DINGTALK_SECRET")
        if env_webhook:
            self.data.setdefault("dingtalk", {})["webhook"] = env_webhook
        if env_secret:
            self.data.setdefault("dingtalk", {})["secret"] = env_secret

        env_rsshub = os.environ.get("RSSHUB_URL")
        if env_rsshub:
            self.data["rsshub_url"] = env_rsshub

        self._validate()

    def _validate(self):
        dt = self.data.get("dingtalk", {})
        if not dt.get("webhook"):
            logger.error("配置错误: dingtalk.webhook 不能为空")
            sys.exit(1)
        if not self.data.get("accounts"):
            logger.error("配置错误: accounts 不能为空")
            sys.exit(1)

    @property
    def rsshub_url(self):
        return self.data.get("rsshub_url", "https://rsshub.app").rstrip("/")

    @property
    def accounts(self):
        return self.data.get("accounts", [])

    @property
    def dingtalk_webhook(self):
        return self.data["dingtalk"]["webhook"]

    @property
    def dingtalk_secret(self):
        return self.data["dingtalk"].get("secret", "")

    @property
    def poll_interval(self):
        return self.data.get("poll_interval_minutes", 5) * 60

    @property
    def state_file(self):
        return self.data.get("state_file", "data/state.json")

    @property
    def send_on_first_run(self):
        return self.data.get("send_on_first_run", False)

    @property
    def max_state_entries(self):
        return self.data.get("max_state_entries", 200)

    @property
    def message_type(self):
        return self.data.get("message_type", "markdown")

    @property
    def dingtalk_keyword(self):
        """钉钉自定义关键词安全设置，所有消息内容中需包含此关键词"""
        return self.data.get("dingtalk", {}).get("keyword", "")

    @property
    def proxy(self):
        """HTTP/HTTPS 代理地址，国内本地测试时需要(如 http://127.0.0.1:7890)"""
        return self.data.get("proxy", "")


# ============================================================
# 状态管理 - 记录已转发的推文ID，防止重复发送
# ============================================================
class StateManager:
    """用 JSON 文件持久化已见推文ID"""

    def __init__(self, state_file, max_entries=200):
        self.state_file = Path(state_file)
        self.max_entries = max_entries
        self.state = self._load()

    def _load(self):
        if self.state_file.exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"状态文件读取失败，将重新创建: {e}")
        return {}

    def save(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)

    def is_seen(self, account, post_id):
        return post_id in self.state.get(account, [])

    def mark_seen(self, account, post_id):
        if account not in self.state:
            self.state[account] = []
        if post_id not in self.state[account]:
            self.state[account].insert(0, post_id)
            self.state[account] = self.state[account][: self.max_entries]

    def is_first_run(self, account):
        return account not in self.state or len(self.state[account]) == 0


# ============================================================
# 钉钉消息发送
# ============================================================
class DingTalkSender:
    """钉钉自定义机器人 Webhook 发送器，支持加签验证"""

    def __init__(self, webhook, secret=""):
        self.webhook = webhook
        self.secret = secret

    def _sign(self):
        """计算加签: timestamp + "\\n" + secret -> HmacSHA256 -> Base64 -> URLEncode"""
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{self.secret}"
        hmac_code = hmac.new(
            self.secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        return timestamp, sign

    def _build_url(self):
        url = self.webhook
        if self.secret:
            timestamp, sign = self._sign()
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}timestamp={timestamp}&sign={sign}"
        return url

    def send_markdown(self, title, text):
        """发送 Markdown 消息"""
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": text},
            "at": {"isAtAll": False},
        }
        return self._send(payload)

    def send_link(self, title, text, message_url, pic_url=""):
        """发送链接卡片消息"""
        payload = {
            "msgtype": "link",
            "link": {
                "title": title,
                "text": text,
                "messageUrl": message_url,
                "picUrl": pic_url,
            },
        }
        return self._send(payload)

    def send_text(self, content):
        """发送纯文本消息"""
        payload = {"msgtype": "text", "text": {"content": content}}
        return self._send(payload)

    def _send(self, payload):
        url = self._build_url()
        try:
            resp = requests.post(url, json=payload, timeout=15)
            result = resp.json()
            if result.get("errcode") == 0:
                return True
            else:
                logger.error(f"钉钉返回错误: {result}")
                return False
        except requests.RequestException as e:
            logger.error(f"钉钉请求失败: {e}")
            return False
        except json.JSONDecodeError:
            logger.error(f"钉钉响应解析失败: {resp.text[:200]}")
            return False


# ============================================================
# RSS 订阅监控
# ============================================================
class RSSMonitor:
    """通过 RSSHub 获取 X 博主的 RSS 订阅源"""

    # 请求头，避免被部分实例拦截
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) X-to-DingTalk/1.0",
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    }

    def __init__(self, rsshub_url, proxy=""):
        self.rsshub_url = rsshub_url.rstrip("/")
        self.proxies = {"http": proxy, "https": proxy} if proxy else None

    def get_rss_url(self, account):
        """根据账号配置生成 RSS URL"""
        if isinstance(account, dict):
            if account.get("rss_url"):
                return account["rss_url"]
            username = account["username"]
        else:
            username = account
        return f"{self.rsshub_url}/twitter/user/{username}"

    def get_display_name(self, account):
        if isinstance(account, dict):
            return account.get("display_name", f"@{account['username']}")
        return f"@{account}"

    def get_account_key(self, account):
        if isinstance(account, dict):
            return account["username"]
        return account

    def fetch_feed(self, account):
        """获取并解析 RSS 订阅源"""
        rss_url = self.get_rss_url(account)
        display_name = self.get_display_name(account)
        logger.info(f"获取订阅: {display_name} -> {rss_url}")

        try:
            resp = requests.get(rss_url, timeout=30, headers=self.HEADERS, proxies=self.proxies)
            if resp.status_code != 200:
                logger.error(
                    f"订阅源返回 HTTP {resp.status_code} - {display_name}"
                )
                return None

            feed = feedparser.parse(resp.content)

            # 检查 RSSHub 错误响应
            if feed.bozo and not feed.entries:
                logger.warning(
                    f"订阅源解析异常，可能 RSSHub 未配置 Twitter 认证 - {display_name}"
                )
                logger.warning(
                    f"请参考文档配置 RSSHub 的 TWITTER_AUTH_TOKEN"
                )
                return None

            if not feed.entries:
                logger.info(f"订阅源无内容 - {display_name}")
                return None

            logger.info(f"获取到 {len(feed.entries)} 条推文 - {display_name}")
            return feed

        except requests.RequestException as e:
            logger.error(f"网络请求失败 - {display_name}: {e}")
            return None

    def parse_entries(self, feed, account):
        """将 feedparser 结果解析为统一格式"""
        display_name = self.get_display_name(account)
        entries = []

        for entry in feed.entries:
            link = entry.get("link", "")

            # 从链接中提取推文ID (如 https://x.com/elonmusk/status/123456)
            post_id = link
            match = re.search(r"/status/(\d+)", link)
            if match:
                post_id = match.group(1)
            elif entry.get("id"):
                post_id = str(entry["id"])

            # 提取推文内容
            title = entry.get("title", "")
            summary = entry.get("summary", entry.get("description", ""))

            # 从 HTML 中提取图片 URL
            images = re.findall(
                r'<img[^>]+src=["\']([^"\']+)["\']', summary
            )

            # 清理 HTML 标签，保留纯文本
            text = re.sub(r"<[^>]+>", "", summary).strip()
            if not text:
                text = title

            # 解析发布时间
            published = entry.get("published", entry.get("updated", ""))
            published_parsed = entry.get("published_parsed")
            if published_parsed:
                try:
                    published = datetime(*published_parsed[:6]).strftime(
                        "%Y-%m-%d %H:%M"
                    )
                except (TypeError, ValueError):
                    pass

            entries.append(
                {
                    "id": post_id,
                    "title": title,
                    "text": text,
                    "link": link,
                    "published": published,
                    "images": images,
                    "author": display_name,
                }
            )

        return entries


# ============================================================
# 消息格式化
# ============================================================
def format_markdown(entry, keyword=""):
    """将推文格式化为钉钉 Markdown 消息，完整展示推文内容"""
    lines = []
    header = f"#### {entry['author']} 发布了新推文"
    if keyword:
        header = f"#### {keyword} | {entry['author']} 发布了新推文"
    lines.append(header)
    lines.append("")

    # 推文完整内容 (不截断，直接展示)
    content = entry["text"]
    lines.append(content)
    lines.append("")

    # 附带图片 (钉钉 Markdown 限制，只放第一张)
    if entry["images"]:
        lines.append(f'![推文图片]({entry["images"][0]})')
        lines.append("")

    # 底部: 发布时间 + 原文链接 (简洁一行)
    time_str = entry["published"] or "未知时间"
    lines.append(f"**{time_str}** | [查看原文]({entry['link']})")

    return "\n".join(lines)


def format_link(entry, keyword=""):
    """将推文格式化为钉钉链接卡片消息"""
    title = f"{entry['author']} 发布了新推文"
    if keyword:
        title = f"{keyword} | {title}"
    text = entry["text"]
    if len(text) > 200:
        text = text[:200] + " ..."
    pic_url = entry["images"][0] if entry["images"] else ""
    return {
        "title": title,
        "text": text,
        "message_url": entry["link"],
        "pic_url": pic_url,
    }


# ============================================================
# 核心转发逻辑
# ============================================================
def check_and_forward(config, state, monitor, sender, once=False):
    """检查所有账号的新推文并转发到钉钉"""
    total_forwarded = 0

    for account in config.accounts:
        account_key = monitor.get_account_key(account)
        display_name = monitor.get_display_name(account)
        first_run = state.is_first_run(account_key)

        feed = monitor.fetch_feed(account)
        if feed is None:
            continue

        entries = monitor.parse_entries(feed, account)
        new_count = 0

        # 逆序处理(旧的先发)，保持时间线顺序
        for entry in reversed(entries):
            if state.is_seen(account_key, entry["id"]):
                continue

            # 首次运行且不发送历史推文: 只记录不发送
            if first_run and not config.send_on_first_run:
                state.mark_seen(account_key, entry["id"])
                continue

            # 发送到钉钉
            success = False
            keyword = config.dingtalk_keyword
            if config.message_type == "link":
                link_data = format_link(entry, keyword)
                success = sender.send_link(**link_data)
            else:
                markdown = format_markdown(entry, keyword)
                title = f"{display_name} 新推文"
                success = sender.send_markdown(title, markdown)

            if success:
                state.mark_seen(account_key, entry["id"])
                new_count += 1
                total_forwarded += 1
                logger.info(
                    f"已转发: {display_name} - 推文ID {entry['id']}"
                )
                # 钉钉限流: 每分钟最多20条，间隔1秒
                time.sleep(1)
            else:
                logger.error(
                    f"转发失败: {display_name} - 推文ID {entry['id']}"
                )

        state.save()

        if first_run and not config.send_on_first_run:
            logger.info(
                f"{display_name}: 首次运行，已记录 {len(entries)} 条历史推文(不发送)"
            )
        elif new_count > 0:
            logger.info(f"{display_name}: 转发了 {new_count} 条新推文")
        else:
            logger.info(f"{display_name}: 暂无新推文")

    return total_forwarded


def run_daemon(config, state, monitor, sender):
    """守护进程模式: 持续轮询"""
    logger.info("=" * 50)
    logger.info("X-to-DingTalk 转发服务已启动 (守护进程模式)")
    logger.info(f"监控账号数: {len(config.accounts)}")
    logger.info(f"轮询间隔: {config.poll_interval} 秒")
    logger.info(f"RSSHub: {config.rsshub_url}")
    logger.info(f"消息类型: {config.message_type}")
    logger.info("=" * 50)

    while True:
        try:
            check_and_forward(config, state, monitor, sender)
        except Exception as e:
            logger.error(f"轮询异常: {e}", exc_info=True)

        logger.info(f"等待 {config.poll_interval} 秒后再次检查...")
        time.sleep(config.poll_interval)


def run_once(config, state, monitor, sender):
    """单次运行模式: 检查一次后退出"""
    logger.info("X-to-DingTalk 单次运行模式")
    total = check_and_forward(config, state, monitor, sender, once=True)
    logger.info(f"完成，共转发 {total} 条推文")


def test_dingtalk(config):
    """测试钉钉 Webhook 连通性"""
    sender = DingTalkSender(config.dingtalk_webhook, config.dingtalk_secret)
    keyword = config.dingtalk_keyword
    test_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = f"{keyword} | X-to-DingTalk 连通性测试" if keyword else "X-to-DingTalk 连通性测试"
    markdown = (
        f"#### {header}\n\n"
        f"> 这是一条测试消息\n\n"
        f"> {test_time}\n\n"
        "> 如果你能看到这条消息，说明钉钉配置正确"
    )
    title = f"{keyword} 连通性测试" if keyword else "连通性测试"
    logger.info("正在发送测试消息到钉钉群...")
    if sender.send_markdown(title, markdown):
        logger.info("测试消息发送成功! 钉钉配置正确")
    else:
        logger.error("测试消息发送失败，请检查 webhook 和 secret 配置")


# ============================================================
# 主入口
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="X(Twitter)博主发文自动转发到钉钉群"
    )
    parser.add_argument(
        "--config",
        default="config.json",
        help="配置文件路径 (默认: config.json)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="单次运行模式 (检查一次后退出，适合 cron)",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="测试钉钉 Webhook 连通性",
    )
    args = parser.parse_args()

    # 加载配置
    config = Config(args.config)

    if args.test:
        test_dingtalk(config)
        return

    # 初始化组件
    state = StateManager(config.state_file, config.max_state_entries)
    monitor = RSSMonitor(config.rsshub_url, config.proxy)
    sender = DingTalkSender(config.dingtalk_webhook, config.dingtalk_secret)

    if args.once:
        run_once(config, state, monitor, sender)
    else:
        run_daemon(config, state, monitor, sender)


if __name__ == "__main__":
    main()
