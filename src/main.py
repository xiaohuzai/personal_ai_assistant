"""
Personal AI Assistant — 入口

加载 .env，配置日志，启动飞书机器人。
"""
import asyncio
import logging
import os

from dotenv import load_dotenv


def main() -> None:
    # 加载 .env（优先于系统环境变量）
    load_dotenv()

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )
    logger = logging.getLogger(__name__)

    # 如果网关使用 ANTHROPIC_AUTH_TOKEN 作为 API key，做一次桥接
    if not os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        os.environ["ANTHROPIC_API_KEY"] = os.environ["ANTHROPIC_AUTH_TOKEN"]
        logger.info("Using ANTHROPIC_AUTH_TOKEN as ANTHROPIC_API_KEY")

    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")

    missing = [k for k, v in {"FEISHU_APP_ID": app_id, "FEISHU_APP_SECRET": app_secret}.items() if not v]
    if missing:
        raise EnvironmentError(f"缺少必要环境变量：{', '.join(missing)}")

    # 初始化：加载持久化 env、配置 lark-cli
    from .agent.assistant import WORKSPACE, initialize
    os.makedirs(WORKSPACE, exist_ok=True)
    asyncio.run(initialize())
    logger.info("Workspace: %s", WORKSPACE)

    from .feishu.bot import start
    start(app_id, app_secret)


if __name__ == "__main__":
    main()
