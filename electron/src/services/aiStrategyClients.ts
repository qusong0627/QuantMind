// AI 策略服务共享 HTTP 客户端
import axios from 'axios';
import { authService } from '../features/auth/services/authService';
import { SERVICE_ENDPOINTS, SERVICE_URLS } from '../config/services';

export const API_BASE_URL = SERVICE_ENDPOINTS.AI_STRATEGY;
export const BACKTEST_API_BASE_URL = SERVICE_URLS.ENGINE_SERVICE;

const resolveBacktestBaseURL = () => String(SERVICE_URLS.ENGINE_SERVICE || '').replace(/\/+$/, '');

export const apiClient = axios.create({
  timeout: 300000,
  headers: {
    'Content-Type': 'application/json',
  },
});

export const backtestClient = axios.create({
  timeout: 180000,
  headers: {
    'Content-Type': 'application/json',
  },
});

apiClient.interceptors.request.use(
  (config) => {
    config.baseURL = resolveAiStrategyBaseURL();
    apiClient.defaults.baseURL = config.baseURL;
    const token = authService.getAccessToken();
    if (token) {
      if (config.headers && typeof config.headers.set === 'function') {
        config.headers.set('Authorization', `Bearer ${token}`);
      } else {
        config.headers.Authorization = `Bearer ${token}`;
      }
    }
    console.log('API请求:', config.method?.toUpperCase(), config.url);
    return config;
  },
  (error) => {
    console.error('API请求错误:', error);
    return Promise.reject(error);
  }
);

apiClient.interceptors.response.use(
  (response) => {
    console.log('API响应:', response.status, response.config.url);
    return response;
  },
  (error) => {
    console.error('API响应错误:', error);
    if (error.response?.status === 401) {
      console.error('认证失败，请检查认证状态');
    }
    return Promise.reject(error);
  }
);

backtestClient.interceptors.request.use(
  (config) => {
    config.baseURL = resolveBacktestBaseURL();
    backtestClient.defaults.baseURL = config.baseURL;
    const token = authService.getAccessToken();
    if (token) {
      if (config.headers && typeof config.headers.set === 'function') {
        config.headers.set('Authorization', `Bearer ${token}`);
      } else {
        config.headers.Authorization = `Bearer ${token}`;
      }
    }
    return config;
  }
);
