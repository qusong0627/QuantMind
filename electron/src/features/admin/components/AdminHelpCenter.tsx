import React from 'react';
import { Typography, Tooltip } from 'antd';
import {
    QuestionCircleOutlined,
    BookOutlined,
    SafetyCertificateOutlined,
    RightOutlined,
    PlayCircleOutlined,
} from '@ant-design/icons';

const { Title, Text, Paragraph } = Typography;

/** 视频教程链接：指向已部署的交互教程站。 */
const VIDEO_TUTORIAL_URL = 'https://quantmindai.cn/tutorial/';

/** 管理后台帮助中心：使用指南与常见问题。 */
export const AdminHelpCenter: React.FC = () => {
    const guides = [
        {
            title: '数据管理',
            desc: '行情数据同步、Qlib 特征引擎、全局股票池与新闻情感模块的使用与调度说明。',
        },
        {
            title: 'API 服务',
            desc: '用户账号与权限管理、策略模板仓库的维护、密钥初始化与轮换流程。',
        },
        {
            title: '推理引擎',
            desc: '模型注册、上线与推理监控，Qlib 回测与 AI 策略生成链路。',
        },
        {
            title: '训练服务',
            desc: '48 维特征字典说明、数据发布（V2 数据源）、AutoDL 训练节点配置。',
        },
        {
            title: '交易核心',
            desc: '订单管理与风险控制，实盘进出场规则的落地与风控阈值调整。',
        },
        {
            title: '系统设置',
            desc: 'FinBERT 情感分析模型开关、系统运行参数与当前部署版本信息。',
        },
    ];

    const faqs: { q: string; a: string; extra?: React.ReactNode }[] = [
        {
            q: '如何判断当前平台是否为最新版本？',
            a: '侧边栏左下角的负载监控卡片上方，以及系统概览页会展示「落后 N 个提交」提示。若显示落后，可在概览页点击「更新系统」一键执行；也可用 SSH 工具登录服务器，手动执行下面这条命令：',
            extra: (
                <Paragraph
                    code
                    copyable={{ text: 'cd /opt/quantmind && sudo bash deploy/update.sh --force' }}
                    className="!mt-2.5 !mb-0 !text-[13px] !text-slate-700 break-all"
                >
                    cd /opt/quantmind && sudo bash deploy/update.sh --force
                </Paragraph>
            ),
        },
        {
            q: '行情数据很久没有更新怎么办？',
            a: '进入「数据管理 → 数据管理」，参考「定时调度与市场数据同步」说明，确认对应市场的同步开关已启用并保存，之后按调度时间自动同步。',
        },
        {
            q: 'FinBERT 情感分析模型在哪里开关？',
            a: '进入「系统设置」页，找到 FinBERT 情感分析开关。开启后模型约需 10-20 秒后台加载，加载完成即为就绪状态。',
        },
        {
            q: '管理后台各服务为什么依赖端口不同？',
            a: '后端为单容器部署：api(8000) 认证/策略/社区、engine(8001) 回测/推理、trade(8002) 订单/风控、stream(8003) 实时行情。统一由 main_oss.py 启动。',
        },
        {
            q: '代码更新后需要重新构建镜像吗？',
            a: '纯代码改动无需重新打包，服务器上执行 git pull && docker compose restart 即可。仅当新增 pip 依赖或升级 torch/qlib 等底层库时才需 docker compose build。',
        },
        {
            q: '遇到问题应如何反馈？',
            a: '可先查看本文档的「使用指南」与「常见问题」，若仍无法解决，请参考下方「技术支持」联系方式提交反馈，并附上浏览器控制台报错与大致操作步骤。',
        },
    ];

    return (
        <div className="p-6 space-y-6 animate-in fade-in slide-in-from-bottom-4 duration-500">
            {/* 页头 */}
            <div className="flex items-center justify-between">
                <div>
                    <Title level={4} className="!m-0 !text-slate-800 flex items-center gap-2">
                        <QuestionCircleOutlined className="text-indigo-600" />
                        帮助中心
                    </Title>
                    <Text className="text-slate-500 text-sm">
                        平台使用指南、常见问题与技术支持入口
                    </Text>
                </div>
                <Tooltip title="在新窗口打开视频教程">
                    <a
                        href={VIDEO_TUTORIAL_URL}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-1.5 rounded-full bg-indigo-600 text-white px-4 py-1.5 text-xs font-bold hover:bg-indigo-700 hover:text-white transition-colors"
                    >
                        <PlayCircleOutlined className="text-sm" />
                        视频教程
                    </a>
                </Tooltip>
            </div>
            {/* 使用指南 */}
            <section>
                <div className="flex items-center gap-2 mb-3">
                    <BookOutlined className="text-slate-700" />
                    <Title level={5} className="!m-0 !text-slate-700">使用指南</Title>
                </div>
                <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
                    {guides.map((g) => (
                        <div
                            key={g.title}
                            className="rounded-2xl border border-slate-200 bg-white p-4 hover:shadow-sm transition-shadow"
                        >
                            <Text strong className="text-slate-800 block text-center mb-2">{g.title}</Text>
                            <Paragraph className="!mb-0 !text-slate-500 !text-sm leading-relaxed">
                                {g.desc}
                            </Paragraph>
                        </div>
                    ))}
                </div>
            </section>

            {/* 常见问题 */}
            <section>
                <div className="flex items-center gap-2 mb-3">
                    <SafetyCertificateOutlined className="text-slate-700" />
                    <Title level={5} className="!m-0 !text-slate-700">常见问题（FAQ）</Title>
                </div>
                <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
                    {faqs.map((f) => (
                        <div
                            key={f.q}
                            className="rounded-2xl border border-slate-200 bg-white p-4 hover:shadow-sm transition-shadow"
                        >
                            <Text strong className="text-slate-800 flex items-start gap-2">
                                <RightOutlined className="mt-1 text-xs text-indigo-500" />
                                {f.q}
                            </Text>
                            <Paragraph className="!mt-2 !mb-0 !text-slate-500 !text-sm leading-relaxed">
                                {f.a}
                            </Paragraph>
                            {f.extra}
                        </div>
                    ))}
                </div>
            </section>
        </div>
    );
};

export default AdminHelpCenter;