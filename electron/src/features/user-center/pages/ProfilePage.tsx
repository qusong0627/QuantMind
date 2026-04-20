/**
 * 个人档案页面
 */

import React, { useState } from 'react';
import { useProfile } from '../hooks';
import { Form, Input, Button, message, Spin, Alert, Upload, Avatar, Modal, Steps, Space } from 'antd';
import { UserOutlined, UploadOutlined } from '@ant-design/icons';
import type { UserProfileUpdate } from '../types';
import { useAuth } from '../../auth/hooks';


interface ProfilePageProps {
  userId: string;
}

const ProfilePage: React.FC<ProfilePageProps> = ({ userId }) => {
  const [form] = Form.useForm();
  const [isEditing, setIsEditing] = useState(false);
  const { user } = useAuth();

  const {
    profile,
    isLoading,
    error,
    updateProfile,
    uploadAvatar,
    avatarUploadStatus,
    avatarUploadProgress,
    updateStatus,
    refetch,
  } = useProfile(userId);

  // 初始化表单数据
  React.useEffect(() => {
    if (profile) {
      form.setFieldsValue({
        username: profile.username || user?.username || '',
        email: profile.email || user?.email || '',
        phone: profile.phone || '',
        bio: profile.bio || '',
        location: profile.location || '',
        website: profile.website || '',
      });
    }
  }, [profile, user, form]);

  const handleSave = async () => {
    try {
      console.log('📝 开始保存档案...');
      const values = await form.validateFields();
      console.log('✅ 表单验证通过:', values);

      await updateProfile(values as UserProfileUpdate);
      console.log('✅ 档案更新成功');

      message.success('档案已更新');
      setIsEditing(false);
    } catch (err: any) {
      console.error('❌ 保存失败:', err);
      message.error(err.message || '更新失败');
    }
  };

  const handleAvatarUpload = async (file: File) => {
    try {
      await uploadAvatar(file);
      message.success('头像上传成功');
      return false; // 阻止默认上传行为
    } catch (err: any) {
      message.error(err.message || '上传失败');
      return false;
    }
  };

  if (isLoading) {
    return (
      <div style={{ textAlign: 'center', padding: 50 }}>
        <Spin size="large" tip="加载中...">
          <div style={{ height: 100 }} />
        </Spin>
      </div>
    );
  }

  if (error) {
    const friendly = error.includes('Network Error')
      ? '网络异常，请稍后重试。'
      : error.includes('未登录')
        ? '登录状态已失效，请重新登录后再试。'
        : error.includes('不存在')
          ? '档案不存在或路径错误，请确认账户是否正确。'
          : error;
    return <Alert message="提示" description={friendly} type="warning" showIcon />;
  }

  if (!profile) {
    return <Alert message="提示" description="暂无档案数据" type="info" showIcon />;
  }

  return (
    <div className="profile-page" style={{ maxWidth: 1200, margin: '0 auto' }}>
      {/* 左右布局 */}
      <div style={{ display: 'flex', gap: 20 }}>
        {/* 左侧：头像和账号信息 */}
        <div style={{ width: 260, flexShrink: 0, display: 'flex', flexDirection: 'column', gap: 20 }}>
          {/* 头像区域 */}
          <div style={{
            textAlign: 'center',
            padding: 24,
            background: '#fff',
            borderRadius: 12,
            border: '1px solid #e2e8f0',
            boxShadow: '0 1px 3px rgba(0,0,0,0.02)'
          }}>
            <Avatar size={90} icon={<UserOutlined />} src={profile.avatar} className="shadow-lg border-4 border-white" />
            <div style={{ marginTop: 16 }}>
              <Upload
                showUploadList={false}
                beforeUpload={(file) => {
                  handleAvatarUpload(file);
                  return false;
                }}
                accept="image/*"
              >
                <Button
                  loading={avatarUploadStatus === 'loading'}
                  className="rounded-xl font-bold border-gray-200 hover:text-blue-500"
                  size="middle"
                >
                  {avatarUploadStatus === 'loading' ? `上传中 ${Math.round(avatarUploadProgress)}%` : '更换头像'}
                </Button>
              </Upload>
            </div>
          </div>

          {/* 账号信息 */}
          <div style={{
            padding: 20,
            background: '#fff',
            borderRadius: 12,
            border: '1px solid #e2e8f0',
            boxShadow: '0 1px 3px rgba(0,0,0,0.02)'
          }}>
            <h4 style={{ marginBottom: 14, fontSize: 13, fontWeight: 800, color: '#1e293b', textTransform: 'uppercase', letterSpacing: '0.05em' }}>账号信息</h4>
            <div style={{ fontSize: 12, lineHeight: 1.8 }}>
              <div className="flex justify-between mb-2">
                <span style={{ color: '#94a3b8', fontWeight: 600 }}>用户ID</span>
                <span className="font-mono text-slate-700 text-xs">{profile.user_id}</span>
              </div>
              <div className="flex justify-between mb-2">
                <span style={{ color: '#94a3b8', fontWeight: 600 }}>交易经验</span>
                <span className="font-bold text-slate-700">
                  {profile.trading_experience === 'beginner'
                    ? '初级'
                    : profile.trading_experience === 'intermediate'
                      ? '中级'
                      : '高级'}
                </span>
              </div>
              <div className="flex justify-between mb-2">
                <span style={{ color: '#94a3b8', fontWeight: 600 }}>注册时间</span>
                <span className="text-slate-700 font-medium text-xs">{new Date(profile.created_at).toLocaleDateString()}</span>
              </div>
              <div className="flex justify-between">
                <span style={{ color: '#94a3b8', fontWeight: 600 }}>更新时间</span>
                <span className="text-slate-700 font-medium text-xs">{new Date(profile.updated_at).toLocaleDateString()}</span>
              </div>
            </div>
          </div>
        </div>

        {/* 右侧：表单区域 */}
        <div style={{
          flex: 1,
          background: '#fff',
          padding: 20,
          borderRadius: 12,
          border: '1px solid #e2e8f0',
          boxShadow: '0 1px 3px rgba(0,0,0,0.02)',
          display: 'flex',
          flexDirection: 'column'
        }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16, flexShrink: 0 }}>
            <h3 style={{ margin: 0, fontSize: 15, fontWeight: 800, color: '#1e293b' }}>基本资料</h3>
            <span style={{ fontSize: 10, color: '#94a3b8', fontWeight: 700, textTransform: 'uppercase' }}>
              {isEditing ? '● Editing Mode' : 'Read Only'}
            </span>
          </div>

          {/* 表单内容 - 不需要滚动 */}
          <div style={{ flex: 1 }}>
            <Form
              form={form}
              layout="vertical"
              size="small"
            >
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 16 }}>
                <Form.Item
                  name="username"
                  label={<span className="text-[10px] font-bold text-slate-400 uppercase tracking-wider">用户名</span>}
                  rules={[{ required: true, message: '请输入用户名' }]}
                  style={{ marginBottom: 12 }}
                >
                  <Input
                    placeholder="请输入用户名"
                    className="rounded-xl border-gray-100 bg-slate-50/50"
                    disabled={!isEditing}
                  />
                </Form.Item>

                <Form.Item
                  name="email"
                  label={<span className="text-[10px] font-bold text-slate-400 uppercase tracking-wider">邮箱</span>}
                  rules={[
                    { required: true, message: '请输入邮箱' },
                    { type: 'email', message: '请输入有效的邮箱地址' },
                  ]}
                  style={{ marginBottom: 12 }}
                >
                  <Input
                    placeholder="请输入邮箱"
                    className="rounded-xl border-gray-100 bg-slate-50/50"
                    disabled={!isEditing}
                  />
                </Form.Item>

              <Form.Item
                name="location"
                label={<span className="text-[10px] font-bold text-slate-400 uppercase tracking-wider">所在地</span>}
                style={{ marginBottom: 12 }}
              >
                  <Input
                    placeholder="例如: 北京"
                    className="rounded-xl border-gray-100 bg-slate-50/50"
                    disabled={!isEditing}
                  />
                </Form.Item>
              </div>

              <Form.Item
                name="website"
                label={<span className="text-[10px] font-bold text-slate-400 uppercase tracking-wider">个人网站</span>}
                style={{ marginBottom: 12 }}
              >
                <Input
                  placeholder="https://"
                  className="rounded-xl border-gray-100 bg-slate-50/50"
                  disabled={!isEditing}
                />
              </Form.Item>

              <Form.Item
                name="bio"
                label={<span className="text-[10px] font-bold text-slate-400 uppercase tracking-wider">个人简介</span>}
                style={{ marginBottom: 12 }}
              >
                <Input.TextArea
                  rows={2}
                  placeholder="介绍一下自己..."
                  className="rounded-xl border-gray-100 bg-slate-50/50"
                  disabled={!isEditing}
                />
              </Form.Item>

              <Form.Item style={{ marginBottom: 0 }}>
                {isEditing ? (
                  <div style={{ display: 'flex', gap: 10 }}>
                    <Button
                      type="primary"
                      onClick={handleSave}
                      loading={updateStatus === 'loading'}
                      size="middle"
                      className="rounded-xl bg-gradient-to-r from-blue-500 to-purple-600 border-none font-bold px-6 shadow-lg shadow-blue-500/20"
                    >
                      保存更新
                    </Button>
                    <Button
                      onClick={() => setIsEditing(false)}
                      size="middle"
                      className="rounded-xl font-bold border-gray-200"
                    >
                      取消
                    </Button>
                  </div>
                ) : (
                  <Button
                    type="primary"
                    size="middle"
                    onClick={() => {
                      console.log('🖊️ 点击编辑档案按钮');
                      setIsEditing(true);
                    }}
                    className="rounded-xl bg-slate-900 border-none font-bold px-6 hover:bg-slate-800 transition-all"
                  >
                    编辑档案
                  </Button>
                )}
              </Form.Item>
            </Form>
          </div>
        </div>
      </div>
    </div>
  );
};

export default ProfilePage;
