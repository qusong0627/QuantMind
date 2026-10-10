#!/usr/bin/env python3
"""
QuantMind服务启动脚本
支持统一配置管理和依赖检查
"""

from config_manager import ConfigManager
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

# 添加配置管理器路径
sys.path.insert(0, str(Path(__file__).parent.parent))


@dataclass
class ServiceStatus:
    """服务状态"""

    name: str
    running: bool
    pid: Optional[int] = None
    port: Optional[int] = None
    start_time: Optional[float] = None


class ServiceManager:
    """服务管理器"""

    def __init__(self):
        self.config_manager = ConfigManager()
        self.services: Dict[str, subprocess.Popen] = {}
        self.logger = self._setup_logger()
        self.running = True

        # 注册信号处理器
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _setup_logger(self) -> logging.Logger:
        """设置日志器"""
        logger = logging.getLogger("service_manager")
        if not logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
            )
            handler.setFormatter(formatter)
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
        return logger

    def _signal_handler(self, signum, frame):
        """信号处理器"""
        self.logger.info(f"Received signal {signum}, shutting down services...")
        self.running = False
        self.stop_all_services()
        sys.exit(0)

    def get_service_dependencies(self, service_name: str) -> List[str]:
        """获取服务依赖"""
        # 服务依赖关系映射
        dependencies = {
            "api-gateway": ["user-service", "ai-strategy-service", "backtest-service"],
            "user-service": [],
            "ai-strategy-service": ["user-service"],
            "backtest-service": ["user-service", "data-service"],
            "data-service": [],
            "market-data-service": [],
            "stock-query-service": ["data-service"],
            "community-service": ["user-service"],
            "dashboard-service": ["user-service", "market-data-service"],
            "notification-service": ["user-service"],
            "data-management-service": ["data-service"],
            "user-center-service": ["user-service"],
        }
        return dependencies.get(service_name, [])

    def check_service_health(
        self, service_name: str, port: int, max_attempts: int = 30
    ) -> bool:
        """检查服务健康状态"""
        import socket

        for attempt in range(max_attempts):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(1)
                result = sock.connect_ex(("localhost", port))
                sock.close()

                if result == 0:
                    self.logger.info(
                        f"Service {service_name} is healthy on port {port}"
                    )
                    return True
                else:
                    self.logger.debug(
                        f"Service {service_name} not ready, attempt {attempt + 1}/{max_attempts}"
                    )

            except Exception as e:
                self.logger.debug(f"Health check failed for {service_name}: {e}")

            if attempt < max_attempts - 1:
                time.sleep(2)

        self.logger.error(
            f"Service {service_name} failed to become healthy after {max_attempts} attempts"
        )
        return False

    def start_service(self, service_name: str) -> bool:
        """启动单个服务"""
        try:
            # 获取服务配置
            service_config = self.config_manager.get_service_config(service_name)

            self.logger.info(f"Starting service: {service_name}")

            # 检查并启动依赖服务
            dependencies = self.get_service_dependencies(service_name)
            for dep_service in dependencies:
                if dep_service not in self.services:
                    self.logger.info(f"Starting dependency service: {dep_service}")
                    if not self.start_service(dep_service):
                        self.logger.error(
                            f"Failed to start dependency service: {dep_service}"
                        )
                        return False
                    time.sleep(2)  # 等待依赖服务启动

                    # 构建启动命令
            project_root = Path(__file__).parent.parent.parent.parent
            service_path = project_root / "backend" / service_name.replace("-", "_")

            if not service_path.exists():
                self.logger.error(f"Service path not found: {service_path}")
                return False

                # 设置环境变量
            env = os.environ.copy()
            env["PYTHONPATH"] = str(project_root) + ":" + env.get("PYTHONPATH", "")

            # 启动服务
            cmd = [sys.executable, "main.py"]
            process = subprocess.Popen(
                cmd,
                cwd=service_path,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
            )

            self.services[service_name] = process
            self.logger.info(f"Service {service_name} started with PID: {process.pid}")

            # 等待服务启动并进行健康检查
            if not self.check_service_health(service_name, service_config.port):
                self.logger.error(f"Service {service_name} failed health check")
                self.stop_service(service_name)
                return False

            return True

        except Exception as e:
            self.logger.error(f"Failed to start service {service_name}: {e}")
            return False

    def stop_service(self, service_name: str):
        """停止单个服务"""
        if service_name in self.services:
            process = self.services[service_name]
            self.logger.info(f"Stopping service: {service_name} (PID: {process.pid})")

            try:
                # 发送SIGTERM信号
                process.terminate()

                # 等待进程结束
                try:
                    process.wait(timeout=10)
                    self.logger.info(f"Service {service_name} stopped gracefully")
                except subprocess.TimeoutExpired:
                    # 强制杀死进程
                    process.kill()
                    process.wait()
                    self.logger.warning(f"Service {service_name} forced to stop")

            except Exception as e:
                self.logger.error(f"Error stopping service {service_name}: {e}")

            del self.services[service_name]

    def restart_service(self, service_name: str) -> bool:
        """重启服务"""
        self.logger.info(f"Restarting service: {service_name}")
        self.stop_service(service_name)
        time.sleep(2)
        return self.start_service(service_name)

    def stop_all_services(self):
        """停止所有服务"""
        self.logger.info("Stopping all services...")
        for service_name in list(self.services.keys()):
            self.stop_service(service_name)

    def get_service_status(self, service_name: str) -> ServiceStatus:
        """获取服务状态"""
        if service_name not in self.services:
            return ServiceStatus(name=service_name, running=False)

        process = self.services[service_name]
        if process.poll() is None:
            # 进程仍在运行
            return ServiceStatus(
                name=service_name,
                running=True,
                pid=process.pid,
                start_time=getattr(process, "start_time", None),
            )
        else:
            # 进程已结束
            return ServiceStatus(name=service_name, running=False)

    def list_services_status(self) -> Dict[str, ServiceStatus]:
        """列出所有服务状态"""
        available_services = self.config_manager.list_available_services()
        status = {}

        for service_name in available_services:
            status[service_name] = self.get_service_status(service_name)

        return status

    def monitor_services(self):
        """监控服务状态"""
        self.logger.info("Starting service monitoring...")

        while self.running:
            try:
                for service_name, process in list(self.services.items()):
                    if process.poll() is not None:
                        self.logger.warning(
                            f"Service {service_name} has stopped, attempting restart..."
                        )
                        self.restart_service(service_name)

                time.sleep(10)  # 每10秒检查一次

            except KeyboardInterrupt:
                break
            except Exception as e:
                self.logger.error(f"Error in service monitoring: {e}")
                time.sleep(5)

    def start_services(self, service_names: List[str]) -> bool:
        """启动多个服务"""
        success = True

        for service_name in service_names:
            if not self.start_service(service_name):
                success = False
                break

        return success


def main():
    """命令行工具入口"""
    import argparse

    parser = argparse.ArgumentParser(description="QuantMind服务管理工具")
    parser.add_argument(
        "action",
        choices=["start", "stop", "restart", "status", "monitor"],
        help="要执行的操作",
    )
    parser.add_argument("services", nargs="*", help="服务名称（留空表示所有服务）")
    parser.add_argument("--environment", "-e", help="环境名称")
    parser.add_argument("--wait", "-w", action="store_true", help="等待服务启动后退出")
    parser.add_argument(
        "--monitor", "-m", action="store_true", help="启动后进入监控模式"
    )

    args = parser.parse_args()

    # 设置环境
    if args.environment:
        os.environ["ENVIRONMENT"] = args.environment

        # 创建服务管理器
    manager = ServiceManager()

    # 获取要操作的服务列表
    if not args.services:
        services = manager.config_manager.list_available_services()
    else:
        services = args.services

        # 执行操作
    if args.action == "start":
        success = manager.start_services(services)
        if not success:
            print("Failed to start some services")
            return 1

        if args.wait:
            print("All services started successfully")
        elif args.monitor:
            manager.monitor_services()

    elif args.action == "stop":
        for service_name in services:
            manager.stop_service(service_name)

    elif args.action == "restart":
        for service_name in services:
            manager.restart_service(service_name)

    elif args.action == "status":
        status = manager.list_services_status()
        print("Service Status:")
        print("-" * 60)
        for service_name, service_status in status.items():
            status_str = "RUNNING" if service_status.running else "STOPPED"
            pid_str = f" (PID: {service_status.pid})" if service_status.pid else ""
            print(f"{service_name:20} {status_str:10} {pid_str}")

    elif args.action == "monitor":
        manager.monitor_services()

    return 0


if __name__ == "__main__":
    exit(main())
