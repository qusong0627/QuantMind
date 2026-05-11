import { apiClient } from './api-client';

export interface SystemCapabilities {
  edition: 'oss' | 'enterprise';
  features: {
    sms: boolean;
    cos: boolean;
    multi_strategy: boolean;
    advanced_factors: boolean;
    rbac_enhanced: boolean;
    audit_logs: boolean;
    local_storage: boolean;
    k8s_deployment: boolean;
  };
}

export const systemService = {
  /**
   * 获取系统能力与版本信息
   */
  getCapabilities: async (): Promise<SystemCapabilities> => {
    return apiClient.get<SystemCapabilities>('/api/v1/system/capabilities');
  }
};
