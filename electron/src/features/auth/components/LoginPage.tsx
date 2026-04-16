/**
 * 登录页面组件
 */

import React, { useState, useEffect, useMemo, useRef } from 'react';
import { Link, useNavigate, useLocation } from 'react-router-dom';
import { Card, Form, Input, Button, Checkbox, Alert, Spin, Typography, Divider, Space, message } from 'antd';
import {
  UserOutlined,
  LockOutlined,
  SafetyCertificateOutlined,
  EyeOutlined,
  EyeInvisibleOutlined,
  MailOutlined,
} from '@ant-design/icons';
import { useAuth, useLoginForm } from '../hooks/useAuth';
import { useAppDispatch } from '../../../store';
import { setUser } from '../store/authSlice';
import { PageLoading } from './LoadingStates';
import type { LoginCredentials } from '../types/auth.types';
import { preloadAiIdeResources } from '../utils/lazyLoad';

const { Title, Text } = Typography;

const LoginPage: React.FC = () => {
  const navigate = useNavigate();
  const location = useLocation();
  const [form] = Form.useForm();
  const { login: handleLogin, isAuthenticated, isLoading } = useAuth();
  const dispatch = useAppDispatch();
  const [lockoutSeconds, setLockoutSeconds] = useState<number>(0);
  const {
    email_or_username,
    password,
    remember_me,
    errors,
    updateField,
    setErrors,
    clearErrors,
    setEmail,
    setPassword,
    setRememberMe,
  } = useLoginForm();

  const [loginError, setLoginError] = useState<string | null>(null);
  const [isInitialLoading, setIsInitialLoading] = useState(true);
  const autoLoginAttempted = useRef(false);

  // 响应式设计
  const [isMobile, setIsMobile] = useState(false);
  const [passwordVisible, setPasswordVisible] = useState(false);

  // 检测移动端
  useEffect(() => {
    const checkMobile = () => {
      setIsMobile(window.innerWidth < 768);
    };

    checkMobile();
    window.addEventListener('resize', checkMobile);

    return () => window.removeEventListener('resize', checkMobile);
  }, []);

  useEffect(() => {
    const stored = localStorage.getItem('auth_lockout_until');
    if (stored) {
      const until = parseInt(stored, 10);
      const now = Date.now();
      if (until > now) {
        setLockoutSeconds(Math.ceil((until - now) / 1000));
      } else {
        localStorage.removeItem('auth_lockout_until');
      }
    }
  }, []);

  useEffect(() => {
    if (lockoutSeconds <= 0) return;
    const timer = setInterval(() => {
      const next = Math.max(lockoutSeconds - 1, 0);
      if (next <= 0) {
        clearInterval(timer);
        localStorage.removeItem('auth_lockout_until');
      }
      setLockoutSeconds(next);
    }, 1000);
    return () => clearInterval(timer);
  }, [lockoutSeconds]);

  // 如果已经登录，重定向到仪表盘或来源页面
  useEffect(() => {
    if (isAuthenticated) {
      const from = (location.state as any)?.from?.pathname || '/';
      navigate(from, { replace: true });
    }
  }, [isAuthenticated, navigate, location.state]);

  useEffect(() => {
    if (autoLoginAttempted.current) return;
    const disabled =
      String((import.meta as any).env?.VITE_DISABLE_AUTH || '').toLowerCase() === 'true';
    if (!disabled) return;

    autoLoginAttempted.current = true;
    (async () => {
      try {
        if (!localStorage.getItem('user')) {
          await handleLogin({
            tenant_id: String((import.meta as any).env?.VITE_TENANT_ID || 'default'),
            email_or_username: 'admin',
            password: '',
            remember_me: true,
          } as LoginCredentials);
        }
      } catch {}
      const from = (location.state as any)?.from?.pathname || '/';
      navigate(from, { replace: true });
    })();
  }, [handleLogin, navigate, location.state]);

  // 初始化加载完成
  useEffect(() => {
    const timer = setTimeout(() => {
      setIsInitialLoading(false);
    }, 100);
    return () => clearTimeout(timer);
  }, []);

  // 登录窗口加载后预加载 AI-IDE 资源（开发/生产统一执行）
  useEffect(() => {
    const trigger = () => {
      void preloadAiIdeResources();
    };

    // 尽量不影响首屏交互：空闲时触发，降级为短延时
    const idleCallback = (window as any).requestIdleCallback as
      | ((cb: () => void, opts?: { timeout: number }) => number)
      | undefined;

    if (typeof idleCallback === 'function') {
      const id = idleCallback(trigger, { timeout: 1200 });
      return () => {
        if (typeof (window as any).cancelIdleCallback === 'function') {
          (window as any).cancelIdleCallback(id);
        }
      };
    }

    const timer = window.setTimeout(trigger, 300);
    return () => window.clearTimeout(timer);
  }, []);

  // 处理登录表单提交
  const handleSubmit = async (values: any) => {
    try {
      setLoginError(null);
      clearErrors();
      
      const credentials: LoginCredentials = {
        tenant_id: String((import.meta as any).env?.VITE_TENANT_ID || 'default'),
        email_or_username: values.email_or_username.trim(),
        password: values.password,
        remember_me: values.remember_me || false,
      };

      await handleLogin(credentials);
      message.success('登录成功！');
      const from = (location.state as any)?.from?.pathname || '/';
      navigate(from, { replace: true });
    } catch (error: any) {
      const errorMessage = error.message || '登录失败，请重试';
      setLoginError(errorMessage);

      // 根据错误类型显示不同的提示
      if (error.message?.includes('账户已被锁定')) {
        message.error(errorMessage, 5);
      } else if (error.message?.includes('剩余尝试次数')) {
        message.warning(errorMessage, 3);
      } else if (error.message?.includes('尝试次数过多')) {
        const match = errorMessage.match(/(\d+)\s*分钟/);
        const minutes = match ? parseInt(match[1], 10) : 10;
        const until = Date.now() + minutes * 60 * 1000;
        localStorage.setItem('auth_lockout_until', String(until));
        setLockoutSeconds(minutes * 60);
        message.error(errorMessage, 3);
      } else {
        message.error(errorMessage, 3);
      }
    }
  };

  // 处理表单字段变化
  const handleFieldChange = (field: string, value: any) => {
    updateField(field, value);
    if (loginError) {
      setLoginError(null);
    }
  };

  // 密码输入框回车提交
  const handlePasswordPressEnter = (e: React.KeyboardEvent) => {
    form.submit();
  };

  // 响应式样式 - 现代化玻璃拟态设计
  const cardStyle = useMemo(() => ({
    width: '100%',
    maxWidth: isMobile ? '100%' : 440,
    borderRadius: isMobile ? '16px 16px 0 0' : '16px',
    background: 'transparent',
    backgroundClip: 'padding-box' as const,
    backdropFilter: 'blur(20px)',
    border: 'none',
    boxShadow: 'none',
    margin: isMobile ? 'auto 0 0 0' : 'auto',
    transform: 'none',
    animation: 'none',
    padding: 0,
    overflow: 'hidden' as const,
  }), [isMobile]);

  // 卡片内部容器样式（负责玻璃拟态效果）
  const cardInnerStyle = useMemo(() => ({
    borderRadius: isMobile ? '16px 16px 0 0' : '16px',
    background: 'rgba(255, 255, 255, 0.95)',
    backgroundClip: 'padding-box' as const,
    backdropFilter: 'blur(20px)',
    border: '1px solid rgba(255, 255, 255, 0.2)',
    boxShadow: isMobile
      ? '0 -10px 40px rgba(0, 0, 0, 0.1)'
      : '0 20px 60px rgba(0, 0, 0, 0.15)',
    padding: isMobile ? '24px 20px' : '32px 40px',
    overflow: 'hidden',
    transform: 'translateZ(0)',
  }), [isMobile]);

  const containerStyle = useMemo(() => ({
    minHeight: '100vh',
    background: `linear-gradient(135deg,
      #667eea 0%,
      #764ba2 25%,
      #f093fb 50%,
      #f5576c 75%,
      #4facfe 100%)`,
    backgroundSize: '400% 400%',
    animation: 'gradientShift 15s ease infinite',
    display: 'flex',
    alignItems: isMobile ? 'flex-end' : 'center',
    justifyContent: 'center',
    padding: isMobile ? 0 : '20px',
    position: 'relative' as const,
  }), [isMobile]);

  // 添加CSS动画
  useEffect(() => {
    const style = document.createElement('style');
    style.textContent = `
      @keyframes gradientShift {
        0% { background-position: 0% 50%; }
        50% { background-position: 100% 50%; }
        100% { background-position: 0% 50%; }
      }

      .submit-button {
        background: linear-gradient(135deg, #1890ff, #722ed1) !important;
        border: none !important;
        position: relative;
        overflow: hidden;
      }

      .submit-button::before {
        content: '';
        position: absolute;
        top: 0;
        left: -100%;
        width: 100%;
        height: 100%;
        background: linear-gradient(90deg, transparent, rgba(255,255,255,0.3), transparent);
        transition: left 0.6s;
      }

      .submit-button:hover::before {
        left: 100%;
      }

      .login-card.ant-card {
        overflow: hidden !important;
        background: transparent !important;
        border: none !important;
      }

      .login-card .ant-card-body {
        overflow: hidden !important;
        background: transparent !important;
      }

      .login-card .login-card-inner {
        -webkit-background-clip: padding-box;
        background-clip: padding-box;
      }
    `;
    document.head.appendChild(style);

    return () => {
      document.head.removeChild(style);
    };
  }, []);

  // 从URL参数获取重定向路径
  const from = (location.state as any)?.from?.pathname || '/user-center';

  // 初始加载状态
  if (isInitialLoading) {
    return <PageLoading message="初始化中..." />;
  }

  return (
    <div style={containerStyle}>
      {/* 登录表单 */}
      <Card 
        className="login-card auth-rounded-card" 
        style={cardStyle}
        styles={{ body: { padding: 0, background: 'transparent', borderRadius: 'inherit', overflow: 'hidden' } }}
      >
        <div className="login-card-inner" style={cardInnerStyle}>
        {/* 页面主标题（卡片顶部居中） */}
        <div style={{
          textAlign: 'center',
          marginBottom: isMobile ? 10 : 18,
        }}>
          <Title level={isMobile ? 4 : 2} style={{ margin: 0, fontWeight: 800, background: 'linear-gradient(90deg, #2f80ed, #7b61ff)', WebkitBackgroundClip: 'text', WebkitTextFillColor: 'transparent', fontSize: isMobile ? 20 : 32, letterSpacing: '-0.5px' } as any}>QuantMind</Title>
          <Text style={{ display: 'block', marginTop: 6, color: 'rgba(0,0,0,0.45)', fontSize: isMobile ? 12 : 14 }}>{'登录系统，轻松开启量化投资之路'}</Text>
        </div>

        <Form
          form={form}
          name="login"
          onFinish={handleSubmit}
          initialValues={{ remember_me: true }}
        >
          {/* 错误提示 */}
          {loginError && (
            <Alert
              message={loginError}
              type="error"
              showIcon
              closable
              onClose={() => setLoginError(null)}
              style={{
                marginBottom: '24px',
                borderRadius: '12px',
                border: 'none',
                background: 'rgba(255, 77, 79, 0.1)',
              }}
            />
          )}

          {lockoutSeconds>0 && (
            <Alert
              type="error"
              showIcon
              style={{ marginBottom: 16 }}
              message={`登录尝试次数过多，请在 ${Math.floor(lockoutSeconds/60)}分${lockoutSeconds%60}秒 后重试`}
            />
          )}
          <Form.Item
            name="email_or_username"
            rules={[
              { required: true, message: '请输入用户名或手机号' },
              { min: 3, message: '用户名或手机号至少3个字符' },
            ]}
            validateStatus={errors.email_or_username ? 'error' : undefined}
            help={errors.email_or_username}
          >
            <Input
              className="form-input"
              prefix={<UserOutlined style={{ color: '#1890ff' }} />}
              placeholder="用户名或手机号"
              size={isMobile ? 'large' : 'large'}
              value={email_or_username}
              onChange={(e) => handleFieldChange('email_or_username', e.target.value)}
              autoComplete="username"
              style={{
                height: isMobile ? '48px' : '56px',
                borderRadius: '12px',
                border: '2px solid #f0f0f0',
                fontSize: isMobile ? '15px' : '16px',
                transition: 'all 0.3s ease',
              }}
            />
          </Form.Item>

          <Form.Item
            name="password"
            rules={[
              { required: true, message: '请输入密码' },
              { min: 6, message: '密码至少6个字符' },
            ]}
            validateStatus={errors.password ? 'error' : undefined}
            help={errors.password}
          >
            <Input.Password
              className="form-input"
              prefix={<LockOutlined style={{ color: '#1890ff' }} />}
              placeholder="密码"
              size={isMobile ? 'large' : 'large'}
              value={password}
              onChange={(e) => handleFieldChange('password', e.target.value)}
              onPressEnter={handlePasswordPressEnter}
              autoComplete="current-password"
              visibilityToggle={{
                visible: passwordVisible,
                onVisibleChange: setPasswordVisible,
              }}
              iconRender={(visible) => (visible ? <EyeOutlined style={{ color: '#1890ff' }} /> : <EyeInvisibleOutlined style={{ color: '#1890ff' }} />)}
              style={{
                height: isMobile ? '48px' : '56px',
                borderRadius: '12px',
                border: '2px solid #f0f0f0',
                fontSize: isMobile ? '15px' : '16px',
                transition: 'all 0.3s ease',
              }}
            />
          </Form.Item>

          <Form.Item>
            <div
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
                flexWrap: isMobile ? 'wrap' : 'nowrap',
                gap: isMobile ? '8px' : '0',
              }}
            >
              <Form.Item name="remember_me" valuePropName="checked" noStyle>
                <Checkbox
                  checked={remember_me}
                  onChange={(e) => handleFieldChange('remember_me', e.target.checked)}
                  style={{ fontSize: isMobile ? '14px' : '15px' }}
                >
                  记住登录
                </Checkbox>
              </Form.Item>
              <Link
                to="/auth/forgot-password"
                style={{
                  fontSize: isMobile ? '14px' : '15px',
                  whiteSpace: 'nowrap',
                  color: '#1890ff',
                  fontWeight: 500,
                }}
              >
                忘记密码？
              </Link>
            </div>
          </Form.Item>

          <Form.Item style={{ marginBottom: '16px' }}>
            <Button
              type="primary"
              htmlType="submit"
              size={isMobile ? 'large' : 'large'}
              block
              loading={isLoading}
              disabled={lockoutSeconds>0}
              className="submit-button"
              style={{
                height: isMobile ? '48px' : '56px',
                borderRadius: '12px',
                fontSize: isMobile ? '16px' : '18px',
                fontWeight: 600,
                boxShadow: '0 4px 20px rgba(24, 144, 255, 0.3)',
                transition: 'all 0.3s ease',
              }}
            >
              {isLoading ? '登录中...' : '立即登录'}
            </Button>
          </Form.Item>
        </Form>

        {/* 注册链接 - 现代化设计 */}
        <div style={{ textAlign: 'center', marginBottom: '24px' }}>
          <Text style={{ fontSize: isMobile ? '14px' : '15px', color: '#666' }}>
            还没有账号？
            <Link
              to="/auth/register"
              style={{
                marginLeft: '8px',
                fontWeight: 600,
                color: '#1890ff',
                textDecoration: 'none',
                transition: 'all 0.3s ease',
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.color = '#722ed1';
                e.currentTarget.style.transform = 'translateX(2px)';
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.color = '#1890ff';
                e.currentTarget.style.transform = 'translateX(0)';
              }}
            >
              立即注册
            </Link>
          </Text>
        </div>

        {/* 安全提示 - 现代化 */}
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontSize: '13px',
            color: '#999',
            marginTop: isMobile ? '20px' : '0',
            padding: '12px',
            background: 'rgba(24, 144, 255, 0.05)',
            borderRadius: '8px',
            border: '1px solid rgba(24, 144, 255, 0.1)',
          }}
        >
          <SafetyCertificateOutlined style={{ marginRight: '6px', color: '#52c41a' }} />
          <span>SSL加密传输 | 数据安全保护</span>
        </div>
        </div>
      </Card>

      {/* 现代化页脚 */}
      <div
        style={{
          position: 'absolute',
          bottom: '0',
          left: '0',
          right: '0',
          textAlign: 'center',
          color: 'white',
          fontSize: '12px',
          padding: '20px',
          background: 'rgba(0, 0, 0, 0.1)',
          backdropFilter: 'blur(10px)',
        }}
      >
        <Space split={<span style={{ color: 'rgba(255,255,255,0.4)', margin: '0 8px' }}>|</span>}>
          <a href="https://www.quantmindai.cn/privacy" target="_blank" rel="noopener noreferrer" style={{ color: 'white', cursor: 'pointer', transition: 'all 0.3s ease', textDecoration: 'none' }} onMouseEnter={(e) => e.currentTarget.style.color = '#1890ff'} onMouseLeave={(e) => e.currentTarget.style.color = 'white'}>隐私政策</a>
          <a href="https://www.quantmindai.cn/terms" target="_blank" rel="noopener noreferrer" style={{ color: 'white', cursor: 'pointer', transition: 'all 0.3s ease', textDecoration: 'none' }} onMouseEnter={(e) => e.currentTarget.style.color = '#1890ff'} onMouseLeave={(e) => e.currentTarget.style.color = 'white'}>服务条款</a>
          <a href="https://www.quantmindai.cn/help" target="_blank" rel="noopener noreferrer" style={{ color: 'white', cursor: 'pointer', transition: 'all 0.3s ease', textDecoration: 'none' }} onMouseEnter={(e) => e.currentTarget.style.color = '#1890ff'} onMouseLeave={(e) => e.currentTarget.style.color = 'white'}>帮助中心</a>
          <span>© 2026 QuantMind</span>
        </Space>
      </div>
    </div>
  );
};

export default LoginPage;
