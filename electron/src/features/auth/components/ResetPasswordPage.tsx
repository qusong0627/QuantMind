/**
 * 重置密码页面组件
 */

import React, { useState, useEffect, useMemo } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import {
  Card,
  Form,
  Input,
  Button,
  Alert,
  Spin,
  Typography,
  Divider,
  message,
  Result,
  Space,
  Progress,
} from 'antd';
import {
  LockOutlined,
  CheckCircleOutlined,
  EyeOutlined,
  EyeInvisibleOutlined,
  InfoCircleOutlined,
} from '@ant-design/icons';
import { authService } from '../services/authService';
import { validatePasswordStrength } from '../utils/validation';
import { PageLoading } from './LoadingStates';
import { handleError } from '../utils/errorHandler';

const { Title, Text } = Typography;

const ResetPasswordPage: React.FC = () => {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [form] = Form.useForm();

  const [isLoading, setIsLoading] = useState(false);
  const [isSuccess, setIsSuccess] = useState(false);
  const [tokenValid, setTokenValid] = useState<boolean | null>(null);
  const [passwordStrength, setPasswordStrength] = useState<any>(null);
  const [passwordVisible, setPasswordVisible] = useState(false);
  const [confirmPasswordVisible, setConfirmPasswordVisible] = useState(false);
  const [isInitialLoading, setIsInitialLoading] = useState(true);
  const [isMobile, setIsMobile] = useState(false);

  const resetToken = searchParams.get('token') || '';

  // 响应式设计
  useEffect(() => {
    const checkMobile = () => {
      setIsMobile(window.innerWidth < 768);
    };

    checkMobile();
    window.addEventListener('resize', checkMobile);
    return () => window.removeEventListener('resize', checkMobile);
  }, []);

  // 验证令牌有效性
  useEffect(() => {
    const validateToken = async () => {
      if (!resetToken) {
        setTokenValid(false);
        setIsInitialLoading(false);
        return;
      }

      // 这里可以添加令牌验证逻辑
      // 暂时假设令牌都是有效的
      setTokenValid(true);
      setIsInitialLoading(false);
    };

    validateToken();
  }, [resetToken]);

  // 初始化加载完成
  useEffect(() => {
    const timer = setTimeout(() => {
      setIsInitialLoading(false);
    }, 100);
    return () => clearTimeout(timer);
  }, []);

  // 密码强度检测
  const checkPasswordStrength = (password: string) => {
    if (!password) {
      setPasswordStrength(null);
      return;
    }

    const result = validatePasswordStrength(password);
    const levels = {
      weak: { color: '#ff4d4f', text: '弱', percent: 20 },
      medium: { color: '#ff7a45', text: '中等', percent: 40 },
      strong: { color: '#52c41a', text: '强', percent: 80 },
      'very-strong': { color: '#1890ff', text: '很强', percent: 100 },
    };

    const level = levels[result.level] || levels.weak;

    setPasswordStrength({
      score: result.score,
      level: result.level,
      color: level.color,
      text: level.text,
      percent: level.percent,
      feedback: result.feedback,
      passed: result.passed,
    });
  };

  // 处理密码变化
  const handlePasswordChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const newPassword = e.target.value;
    checkPasswordStrength(newPassword);

    // 实时验证确认密码
    const confirmPassword = form.getFieldValue('confirmPassword');
    if (confirmPassword && newPassword !== confirmPassword) {
      form.setFields([
        {
          name: 'confirmPassword',
          errors: ['两次输入的密码不一致']
        }
      ]);
    } else {
      form.setFields([
        {
          name: 'confirmPassword',
          errors: []
        }
      ]);
    }
  };

  // 处理确认密码变化
  const handleConfirmPasswordChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const newPassword = form.getFieldValue('newPassword');
    const newConfirmPassword = e.target.value;

    if (newPassword && newConfirmPassword !== newPassword) {
      form.setFields([
        {
          name: 'confirmPassword',
          errors: ['两次输入的密码不一致']
        }
      ]);
    } else {
      form.setFields([
        {
          name: 'confirmPassword',
          errors: []
        }
      ]);
    }
  };

  // 处理表单提交
  const handleSubmit = async (values: { newPassword: string; confirmPassword: string }) => {
    try {
      setIsLoading(true);

      // 验证密码强度
      if (!passwordStrength || !passwordStrength.passed) {
        message.error('密码强度不足，请设置更复杂的密码', 3);
        return;
      }

      // 验证密码匹配
      if (values.newPassword !== values.confirmPassword) {
        message.error('两次输入的密码不一致', 3);
        return;
      }

      // 调用API
      await authService.resetPassword(resetToken, values.newPassword);

      // 成功处理
      setIsSuccess(true);
      message.success('密码重置成功，请使用新密码登录', 3);

    } catch (error: any) {
      // 统一错误处理
      const standardError = handleError(error, { context: 'reset_password' });

      // 特殊错误处理
      if (error.message?.includes('过期') || error.message?.includes('invalid')) {
        setTokenValid(false);
        message.error('重置链接已过期或无效，请重新申请', 5);
      }
    } finally {
      setIsLoading(false);
    }
  };

  // 跳转到登录页面
  const handleGoToLogin = () => {
    navigate('/auth/login');
  };

  // 重新申请重置
  const handleRequestNewReset = () => {
    navigate('/auth/forgot-password');
  };

  // 响应式样式
  const cardStyle = useMemo(() => ({
    width: '100%',
    maxWidth: isMobile ? '100%' : 400,
    borderRadius: isMobile ? 0 : '12px',
    boxShadow: isMobile ? 'none' : '0 8px 32px rgba(0, 0, 0, 0.1)',
    margin: isMobile ? 0 : 'auto',
  }), [isMobile]);

  const containerStyle = useMemo(() => ({
    minHeight: '100vh',
    background: 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    padding: isMobile ? 0 : '20px',
  }), [isMobile]);

  // 初始加载状态
  if (isInitialLoading) {
    return <PageLoading message="验证重置链接中..." />;
  }

  // 令牌无效
  if (tokenValid === false) {
    return (
      <div style={containerStyle}>
        <Card
          className="auth-rounded-card"
          style={cardStyle}
          styles={{
            body: {
              padding: isMobile ? '24px 20px' : '40px',
              borderRadius: 'inherit',
              overflow: 'hidden',
            }
          }}
        >
          <Result
            status="error"
            title="重置链接无效"
            subTitle="该重置链接已过期或无效，请重新申请密码重置"
            extra={[
              <Button
                key="request"
                type="primary"
                onClick={handleRequestNewReset}
                style={{
                  background: 'linear-gradient(135deg, #1890ff, #722ed1)',
                  border: 'none',
                }}
              >
                重新申请重置
              </Button>,
              <Button key="login" onClick={handleGoToLogin}>
                返回登录
              </Button>,
            ]}
          />
        </Card>
      </div>
    );
  }

  return (
    <div style={containerStyle}>
      <Card
        className="auth-rounded-card"
        style={cardStyle}
        styles={{
          body: {
            padding: isMobile ? '24px 20px' : '40px',
            borderRadius: 'inherit',
            overflow: 'hidden',
          }
        }}
      >
        {isSuccess ? (
          /* 成功状态 */
          <Result
            status="success"
            icon={<CheckCircleOutlined style={{ color: '#52c41a' }} />}
            title="密码重置成功"
            subTitle="您的密码已成功重置，请使用新密码登录"
            extra={[
              <Button
                key="login"
                type="primary"
                onClick={handleGoToLogin}
                style={{
                  background: 'linear-gradient(135deg, #1890ff, #722ed1)',
                  border: 'none',
                }}
              >
                立即登录
              </Button>,
            ]}
          />
        ) : (
          /* 表单状态 */
          <>
            {/* Logo 和标题 */}
            <div style={{ textAlign: 'center', marginBottom: isMobile ? '24px' : '32px' }}>
              <div
                style={{
                  width: isMobile ? '48px' : '64px',
                  height: isMobile ? '48px' : '64px',
                  background: 'linear-gradient(135deg, #1890ff, #722ed1)',
                  borderRadius: '50%',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  margin: '0 auto 16px',
                  fontSize: isMobile ? '18px' : '24px',
                  color: 'white',
                  fontWeight: 'bold',
                }}
              >
                QM
              </div>
              <Title
                level={isMobile ? 4 : 3}
                style={{
                  margin: 0,
                  color: '#262626',
                  fontSize: isMobile ? '20px' : '24px'
                }}
              >
                重置密码
              </Title>
              <Text type="secondary" style={{ fontSize: '14px' }}>
                请设置您的新密码
              </Text>
            </div>

            {/* 安全提示 */}
            <div
              style={{
                background: '#f8f9fa',
                border: '1px solid #e9ecef',
                borderRadius: '6px',
                padding: '12px 16px',
                marginBottom: '24px',
              }}
            >
              <Space>
                <InfoCircleOutlined style={{ color: '#1890ff' }} />
                <Text style={{ fontSize: '13px', color: '#666' }}>
                  请设置一个包含大小写字母、数字和特殊字符的强密码
                </Text>
              </Space>
            </div>

            {/* 重置密码表单 */}
            <Form
              form={form}
              name="resetPassword"
              onFinish={handleSubmit}
              layout="vertical"
              requiredMark={false}
              disabled={isLoading}
              size={isMobile ? 'middle' : 'large'}
            >
              <Form.Item
                name="newPassword"
                rules={[
                  { required: true, message: '请输入新密码' },
                  { min: 8, message: '密码至少8个字符' },
                  {
                    validator: (_, value) => {
                      const result = validatePasswordStrength(value);
                      return result.passed ? Promise.resolve() : Promise.reject(result.feedback[0]);
                    }
                  }
                ]}
              >
                <Input.Password
                  prefix={<LockOutlined />}
                  placeholder="新密码"
                  size={isMobile ? 'large' : 'large'}
                  onChange={handlePasswordChange}
                  autoComplete="new-password"
                  visibilityToggle={{
                    visible: passwordVisible,
                    onVisibleChange: setPasswordVisible,
                  }}
                  iconRender={(visible) => (visible ? <EyeOutlined /> : <EyeInvisibleOutlined />)}
                />
              </Form.Item>

              {/* 密码强度指示器 */}
              {passwordStrength && (
                <div style={{ marginBottom: '16px' }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '4px' }}>
                    <Text style={{ fontSize: '12px', color: '#666' }}>密码强度</Text>
                    <Text style={{ fontSize: '12px', color: passwordStrength.color }}>
                      {passwordStrength.text}
                    </Text>
                  </div>
                  <Progress
                    percent={passwordStrength.percent}
                    strokeColor={passwordStrength.color}
                    showInfo={false}
                    size="small"
                    style={{ marginBottom: '8px' }}
                  />
                  {passwordStrength.feedback && passwordStrength.feedback.length > 0 && (
                    <div style={{ fontSize: '12px', color: '#ff4d4f' }}>
                      • {passwordStrength.feedback.join(' • ')}
                    </div>
                  )}
                </div>
              )}

              <Form.Item
                name="confirmPassword"
                dependencies={['newPassword']}
                rules={[
                  { required: true, message: '请确认新密码' },
                  ({ getFieldValue }) => ({
                    validator(_, value) {
                      if (!value || getFieldValue('newPassword') === value) {
                        return Promise.resolve();
                      }
                      return Promise.reject('两次输入的密码不一致');
                    },
                  }),
                ]}
              >
                <Input.Password
                  prefix={<LockOutlined />}
                  placeholder="确认新密码"
                  size={isMobile ? 'large' : 'large'}
                  onChange={handleConfirmPasswordChange}
                  autoComplete="new-password"
                  visibilityToggle={{
                    visible: confirmPasswordVisible,
                    onVisibleChange: setConfirmPasswordVisible,
                  }}
                  iconRender={(visible) => (visible ? <EyeOutlined /> : <EyeInvisibleOutlined />)}
                />
              </Form.Item>

              <Form.Item style={{ marginBottom: '16px' }}>
                <Button
                  type="primary"
                  htmlType="submit"
                  size={isMobile ? 'large' : 'large'}
                  block
                  loading={isLoading}
                  style={{
                    height: isMobile ? '44px' : '48px',
                    borderRadius: '8px',
                    background: 'linear-gradient(135deg, #1890ff, #722ed1)',
                    border: 'none',
                    fontSize: isMobile ? '16px' : '16px',
                    fontWeight: 'bold',
                  }}
                >
                  {isLoading ? '重置中...' : '重置密码'}
                </Button>
              </Form.Item>
            </Form>

            <Divider style={{ margin: '24px 0' }}>
              <Text type="secondary" style={{ fontSize: '14px' }}>
                或
              </Text>
            </Divider>

            {/* 返回登录链接 */}
            <div style={{ textAlign: 'center' }}>
              <Text style={{ fontSize: '14px', color: '#666' }}>
                记起密码了？
                <Link to="/auth/login" style={{ marginLeft: '8px', fontWeight: 'bold' }}>
                  立即登录
                </Link>
              </Text>
            </div>
          </>
        )}
      </Card>

      {!isMobile && (
        /* 页面底部 */
        <div
          style={{
            position: 'absolute',
            bottom: '20px',
            left: '0',
            right: '0',
            textAlign: 'center',
            color: 'white',
            fontSize: '12px',
          }}
        >
          <Space split={<span style={{ color: 'rgba(255,255,255,0.3)' }}>|</span>}>
            <a href="https://api.quantmind.cloud/privacy" target="_blank" rel="noopener noreferrer" style={{ color: 'white', textDecoration: 'none' }}>隐私政策</a>
            <a href="https://api.quantmind.cloud/terms" target="_blank" rel="noopener noreferrer" style={{ color: 'white', textDecoration: 'none' }}>服务条款</a>
            <a href="https://api.quantmind.cloud/help" target="_blank" rel="noopener noreferrer" style={{ color: 'white', textDecoration: 'none' }}>帮助中心</a>
            <span>© 2026 QuantMind</span>
          </Space>
        </div>
      )}
    </div>
  );
};

export default ResetPasswordPage;
