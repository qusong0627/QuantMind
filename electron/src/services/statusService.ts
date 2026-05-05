// 服务状态检测服务
import axios from 'axios';
import { SERVICE_PORTS, getDynamicServerUrl } from '../config/services';

export interface ServiceStatus {
  name: string;
  port: number;
  status: 'online' | 'offline' | 'checking';
  lastChecked: Date;
  responseTime?: number;
  error?: string;
}

class StatusService {
  private static instance: StatusService;
  private statusCache: Map<string, ServiceStatus> = new Map();
  private checkInterval: NodeJS.Timeout | null = null;
  private listeners: Set<(statuses: Map<string, ServiceStatus>) => void> = new Set();

  static getInstance(): StatusService {
    if (!StatusService.instance) {
      StatusService.instance = new StatusService();
    }
    return StatusService.instance;
  }

  constructor() {
    this.startAutoCheck();
  }

  // 要检测的服务列表 - 使用统一端口配置
  private services = [
    { name: 'AI策略服务', port: SERVICE_PORTS.AI_STRATEGY, path: '/health' },
    { name: 'API网关', port: SERVICE_PORTS.API_GATEWAY, path: '/health' },
    { name: '用户服务', port: SERVICE_PORTS.USER_SERVICE, path: '/health' },
    { name: '数据服务', port: SERVICE_PORTS.DATA_SERVICE, path: '/health' },
    { name: '市场数据服务', port: SERVICE_PORTS.MARKET_DATA, path: '/health' },
    { name: 'Qlib回测服务', port: SERVICE_PORTS.QLIB_SERVICE, path: '/health' },
    { name: '股票查询服务', port: SERVICE_PORTS.STOCK_QUERY, path: '/health' },
  ];

  // 检测单个服务状态
  private async checkServiceStatus(port: number, path: string = '/'): Promise<ServiceStatus> {
    const startTime = Date.now();
    const dynamicUrl = getDynamicServerUrl();
    const baseUrl = dynamicUrl || `http://localhost:${port}`;
    
    // 确保 URL 包含协议，且避免重复端口（如果 dynamicUrl 已经包含了端口）
    const finalUrl = baseUrl.startsWith('http') 
      ? `${baseUrl.replace(/\/+$/, '')}${path}`
      : `http://${baseUrl.replace(/\/+$/, '')}${path}`;

    try {
      const response = await axios.get(finalUrl, {
        timeout: 5000,
        validateStatus: (status) => status < 500, // 接受2xx, 3xx, 4xx状态码
      });

      const responseTime = Date.now() - startTime;

      return {
        name: this.getServiceName(port),
        port,
        status: 'online',
        lastChecked: new Date(),
        responseTime,
      };
    } catch (error) {
      return {
        name: this.getServiceName(port),
        port,
        status: 'offline',
        lastChecked: new Date(),
        error: error instanceof Error ? error.message : '连接失败',
      };
    }
  }

  // 根据端口获取服务名称
  private getServiceName(port: number): string {
    const service = this.services.find(s => s.port === port);
    return service?.name || `端口${port}`;
  }

  // 检测所有服务状态
  async checkAllServices(): Promise<Map<string, ServiceStatus>> {
    const promises = this.services.map(async (service) => {
      const status = await this.checkServiceStatus(service.port, service.path);
      return [`${service.port}`, status] as [string, ServiceStatus];
    });

    const results = await Promise.all(promises);
    const newStatuses = new Map(results);

    // 更新缓存
    this.statusCache = newStatuses;

    // 通知监听器
    this.notifyListeners();

    return newStatuses;
  }

  // 获取特定服务状态
  async checkService(port: number): Promise<ServiceStatus> {
    const service = this.services.find(s => s.port === port);
    const path = service?.path || '/';

    const status = await this.checkServiceStatus(port, path);
    this.statusCache.set(`${port}`, status);
    this.notifyListeners();

    return status;
  }

  // 获取当前状态缓存
  getCurrentStatuses(): Map<string, ServiceStatus> {
    return new Map(this.statusCache);
  }

  // 获取AI策略服务状态
  async getAIStrategyStatus(): Promise<ServiceStatus> {
    return this.checkService(SERVICE_PORTS.AI_STRATEGY);
  }

  // 检查AI策略服务是否在线
  async isAIStrategyOnline(): Promise<boolean> {
    const status = await this.getAIStrategyStatus();
    return status.status === 'online';
  }

  // 获取数据服务状态
  async getDataServiceStatus(): Promise<ServiceStatus> {
    return this.checkService(SERVICE_PORTS.DATA_SERVICE);
  }

  // 检查数据服务是否在线
  async isDataServiceOnline(): Promise<boolean> {
    const status = await this.getDataServiceStatus();
    return status.status === 'online';
  }

  // 添加状态变化监听器
  addListener(listener: (statuses: Map<string, ServiceStatus>) => void): void {
    this.listeners.add(listener);
    // 立即通知当前状态
    listener(this.getCurrentStatuses());
  }

  // 移除监听器
  removeListener(listener: (statuses: Map<string, ServiceStatus>) => void): void {
    this.listeners.delete(listener);
  }

  // 通知所有监听器
  private notifyListeners(): void {
    this.listeners.forEach(listener => {
      try {
        listener(this.getCurrentStatuses());
      } catch (error) {
        console.error('状态监听器错误:', error);
      }
    });
  }

  // 开始自动检测
  private startAutoCheck(): void {
    // 立即检测一次
    this.checkAllServices();

    // 每30秒检测一次
    this.checkInterval = setInterval(() => {
      this.checkAllServices();
    }, 30000);
  }

  // 停止自动检测
  stopAutoCheck(): void {
    if (this.checkInterval) {
      clearInterval(this.checkInterval);
      this.checkInterval = null;
    }
  }

  // 手动刷新状态
  async refresh(): Promise<void> {
    await this.checkAllServices();
  }

  // 获取服务摘要信息
  getSummary(): {
    total: number;
    online: number;
    offline: number;
    services: ServiceStatus[];
  } {
    const statuses = Array.from(this.statusCache.values());
    const online = statuses.filter(s => s.status === 'online').length;
    const offline = statuses.filter(s => s.status === 'offline').length;

    return {
      total: statuses.length,
      online,
      offline,
      services: statuses,
    };
  }

  // 清理资源
  dispose(): void {
    this.stopAutoCheck();
    this.listeners.clear();
    this.statusCache.clear();
  }
}

// 导出单例实例
export const statusService = StatusService.getInstance();

// React Hook 已移至 hooks/useServiceStatus.ts
