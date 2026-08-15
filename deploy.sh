#!/bin/bash
# ============================================================
# X-to-DingTalk 一键部署脚本 (Ubuntu)
# ============================================================
# 使用方法:
#   1. 将整个 x-to-dingtalk 目录上传到服务器
#   2. cd x-to-dingtalk
#   3. chmod +x deploy.sh && ./deploy.sh
# ============================================================

set -e

echo "================================================"
echo "  X-to-DingTalk 部署脚本"
echo "================================================"

# ---- 检查系统 ----
if [ -f /etc/os-release ]; then
    . /etc/os-release
    echo "系统: $NAME $VERSION"
fi

# ---- 检查 Docker ----
echo ""
echo "[1/5] 检查 Docker..."
if command -v docker &> /dev/null; then
    echo "  Docker 已安装: $(docker --version)"
else
    echo "  Docker 未安装，正在安装..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
    echo "  Docker 安装完成: $(docker --version)"
fi

# ---- 检查 Docker Compose ----
echo ""
echo "[2/5] 检查 Docker Compose..."
if docker compose version &> /dev/null; then
    echo "  Docker Compose 可用: $(docker compose version)"
elif command -v docker-compose &> /dev/null; then
    echo "  Docker Compose 可用: $(docker-compose --version)"
    echo "  提示: 建议升级到新版 docker compose 插件"
else
    echo "  Docker Compose 未安装，正在安装插件..."
    apt-get update -qq && apt-get install -y -qq docker-compose-plugin
    echo "  Docker Compose 安装完成"
fi

# ---- 检查配置文件 ----
echo ""
echo "[3/5] 检查配置文件..."

if [ ! -f .env ]; then
    cp .env.example .env
    echo "  已创建 .env 文件"
    echo "  ⚠️  请编辑 .env 填入 Twitter Cookie!"
    echo "      命令: nano .env"
else
    echo "  .env 已存在"
fi

if [ ! -f config.json ]; then
    cp config.example.json config.json
    echo "  已创建 config.json 文件"
    echo "  ⚠️  请编辑 config.json 填入钉钉和监控账号!"
    echo "      命令: nano config.json"
else
    echo "  config.json 已存在"
fi

# ---- 检查必填项 ----
echo ""
echo "[4/5] 检查配置完整性..."

# 检查 Twitter Cookie
if grep -q "在这里填入" .env 2>/dev/null; then
    echo "  ⚠️  .env 中的 Twitter Cookie 未填写!"
    echo "      请先填写后再运行部署"
    NEED_CONFIG=1
else
    echo "  Twitter Cookie: 已配置"
fi

# 检查钉钉 Webhook
if grep -q "YOUR_ACCESS_TOKEN" config.json 2>/dev/null; then
    echo "  ⚠️  config.json 中的钉钉 Webhook 未填写!"
    echo "      请先填写后再运行部署"
    NEED_CONFIG=1
else
    echo "  钉钉 Webhook: 已配置"
fi

if [ "$NEED_CONFIG" = "1" ]; then
    echo ""
    echo "================================================"
    echo "  配置未完成，请按上方提示填写配置后重新运行"
    echo "  nano .env        # 填入 Twitter Cookie"
    echo "  nano config.json # 填入钉钉和监控账号"
    echo "================================================"
    exit 1
fi

# ---- 启动服务 ----
echo ""
echo "[5/5] 启动 Docker 服务..."
docker compose up -d --build

echo ""
echo "================================================"
echo "  部署完成!"
echo "================================================"
echo ""
echo "常用命令:"
echo "  查看日志:     docker compose logs -f x-to-dingtalk"
echo "  查看状态:     docker compose ps"
echo "  重启服务:     docker compose restart"
echo "  停止服务:     docker compose down"
echo "  测试钉钉:     docker compose exec x-to-dingtalk python main.py --test"
echo ""
echo "RSSHub 调试: http://服务器IP:1200"
echo ""
