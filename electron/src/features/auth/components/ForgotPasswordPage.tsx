/**
 * 忘记密码页面组件
 */

import React, { useState, useEffect, useMemo } from 'react';
import { Link, useNavigate } from 'react-router-dom';
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
} from 'antd';
import {
  MailOutlined,
  ArrowLeftOutlined,
  CheckCircleOutlined,
  InfoCircleOutlined,
} from '@ant-design/icons';
import { authService } from '../services/authService';
import { validateEmail } from '../utils/validation';
import { PageLoading } from './LoadingStates';
import { handleError } from '../utils/errorHandler';
import HelpCenterLink from '../../../components/common/HelpCenterLink';

const { Title, Text } = Typography;

const ForgotPasswordPage: React.FC = () => {
  const navigate = useNavigate();
  const [form] = Form.useForm();

  const [isLoading, setIsLoading] = useState(false);
  const [isSuccess, setIsSuccess] = useState(false);
  const [email, setEmail] = useState('');
  const [isInitialLoading, setIsInitialLoading] = useState(true);
  const [isMobile, setIsMobile] = useState(false);

  // 响应式设计
  useEffect(() => {
    const checkMobile = () => {
      setIsMobile(window.innerWidth < 768);
    };

    checkMobile();
    window.addEventListener('resize', checkMobile);
    return () => window.removeEventListener('resize', checkMobile);
  }, []);

  // 初始化加载完成
  useEffect(() => {
    const timer = setTimeout(() => {
      setIsInitialLoading(false);
    }, 100);
    return () => clearTimeout(timer);
  }, []);

  // 处理表单提交
  const handleSubmit = async (values: { email: string }) => {
    try {
      setIsLoading(true);

      // 客户端验证
      const emailValidation = validateEmail(values.email);
      if (!emailValidation.valid) {
        message.error(emailValidation.message || '请输入有效的邮箱地址');
        return;
      }

      // 调用API
      await authService.forgotPassword(values.email.trim());

      // 成功处理
      setEmail(values.email.trim());
      setIsSuccess(true);
      message.success('重置邮件已发送，请检查您的邮箱', 5);

    } catch (error: any) {
      // 统一错误处理
      const standardError = handleError(error, { context: 'forgot_password' });

      // 如果是网络错误，显示特殊提示
      if (error.code === 'NETWORK_ERROR') {
        message.warning('网络连接异常，请稍后重试', 4);
      }
    } finally {
      setIsLoading(false);
    }
  };

  // 重新发送邮件
  const handleResendEmail = async () => {
    try {
      setIsLoading(true);
      await authService.forgotPassword(email);
      message.success('重置邮件已重新发送', 3);
    } catch (error: any) {
      handleError(error, { context: 'resend_reset_email' });
    } finally {
      setIsLoading(false);
    }
  };

  // 返回登录页面
  const handleBackToLogin = () => {
    navigate('/auth/login');
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
    return <PageLoading message="初始化中..." />;
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
        {/* 返回按钮 */}
        {!isMobile && (
          <Button
            type="text"
            icon={<ArrowLeftOutlined />}
            onClick={handleBackToLogin}
            style={{
              marginBottom: '24px',
              padding: '4px 8px',
              color: '#666',
            }}
          >
            返回登录
          </Button>
        )}

        {isSuccess ? (
          /* 成功状态 */
          <Result
            status="success"
            icon={<CheckCircleOutlined style={{ color: '#52c41a' }} />}
            title="邮件发送成功"
            subTitle={
              <div style={{ marginTop: '16px' }}>
                <Text style={{ fontSize: '16px', color: '#262626', display: 'block', marginBottom: '8px' }}>
                  我们已向 <Text strong>{email}</Text> 发送了密码重置邮件
                </Text>
                <Text style={{ fontSize: '14px', color: '#666', display: 'block' }}>
                  请检查您的邮箱（包括垃圾邮件文件夹），并点击邮件中的链接来重置密码
                </Text>
              </div>
            }
            extra={[
              <Button
                key="resend"
                type="primary"
                onClick={handleResendEmail}
                loading={isLoading}
                style={{
                  background: 'linear-gradient(135deg, #1890ff, #722ed1)',
                  border: 'none',
                }}
              >
                重新发送邮件
              </Button>,
              <Button key="login" onClick={handleBackToLogin}>
                返回登录
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
                忘记密码
              </Title>
              <Text type="secondary" style={{ fontSize: '14px' }}>
                输入您的邮箱地址，我们将发送重置密码的链接
              </Text>
            </div>

            {/* 说明信息 */}
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
                  重置链接将在24小时后失效，请及时处理
                </Text>
              </Space>
            </div>

            {/* 忘记密码表单 */}
            <Form
              form={form}
              name="forgotPassword"
              onFinish={handleSubmit}
              layout="vertical"
              requiredMark={false}
              disabled={isLoading}
              size={isMobile ? 'middle' : 'large'}
            >
              <Form.Item
                name="email"
                rules={[
                  { required: true, message: '请输入邮箱地址' },
                  { type: 'email', message: '请输入有效的邮箱地址' },
                ]}
              >
                <Input
                  prefix={<MailOutlined />}
                  placeholder="请输入注册时使用的邮箱地址"
                  size={isMobile ? 'large' : 'large'}
                  autoComplete="email"
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
                  {isLoading ? '发送中...' : '发送重置邮件'}
                </Button>
              </Form.Item>
            </Form>

            <Divider style={{ margin: '24px 0' }}>
              <Text type="secondary" style={{ fontSize: '14px' }}>
                或
              </Text>
            </Divider>

            {/* 登录链接 */}
            <div style={{ textAlign: 'center' }}>
              <Text style={{ fontSize: '14px', color: '#666' }}>
                记起密码了？
                <Link to="/auth/login" style={{ marginLeft: '8px', fontWeight: 'bold' }}>
                  立即登录
                </Link>
              </Text>
            </div>

            {/* 移动端返回按钮 */}
            {isMobile && (
              <div style={{ textAlign: 'center', marginTop: '24px' }}>
                <Button
                  type="text"
                  icon={<ArrowLeftOutlined />}
                  onClick={handleBackToLogin}
                  style={{ color: '#666' }}
                >
                  返回登录
                </Button>
              </div>
            )}
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
            <a href="https://www.quantmindai.cn/privacy" target="_blank" rel="noopener noreferrer" style={{ color: 'white', textDecoration: 'none' }}>隐私政策</a>
            <a href="https://www.quantmindai.cn/terms" target="_blank" rel="noopener noreferrer" style={{ color: 'white', textDecoration: 'none' }}>服务条款</a>
            <HelpCenterLink variant="white" />
            <span>© 2026 QuantMind</span>
          </Space>
        </div>
      )}
    </div>
  );
};

export default ForgotPasswordPage;
