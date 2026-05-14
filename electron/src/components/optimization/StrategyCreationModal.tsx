import React, { useState } from 'react';
import { Save, X, Code, CheckCircle, FileText } from 'lucide-react';
// backtestService will be loaded dynamically when saving
import { GridSearchConfig } from './ParameterGrid';

interface Props {
    isOpen: boolean;
    onClose: () => void;
    bestParams: Record<string, any>;
    strategyType: 'TopkDropout' | 'Weighted';
    config: GridSearchConfig; // Or GeneticConfig in future, simplified for now
}

export const StrategyCreationModal: React.FC<Props> = ({ isOpen, onClose, bestParams, strategyType }) => {
    const [name, setName] = useState('');
    const [description, setDescription] = useState('');
    const [isSaving, setIsSaving] = useState(false);
    const [saveSuccess, setSaveSuccess] = useState(false);
    const [error, setError] = useState('');

    if (!isOpen) return null;

    const generateCode = () => {
        const timestamp = new Date().toISOString().split('T')[0];

        if (strategyType === 'TopkDropout') {
            const topk = bestParams.topk || 50;
            const n_drop = bestParams.n_drop || 5;

            return `"""
策略名称: ${name}
策略描述: ${description || '由参数优化实验室自动生成'}
创建时间: ${timestamp}
来源: 自动生成 (Grid Search)
最佳参数: TopK=${topk}, N_Drop=${n_drop}
"""

TOPK = ${topk}
N_DROP = ${n_drop}

STRATEGY_CONFIG = {
    "class": "TopkDropoutStrategy",
    "module_path": "qlib.contrib.strategy.signal_strategy",
    "kwargs": {
        "signal": "<PRED>",
        "topk": TOPK,
        "n_drop": N_DROP,
    },
}
`;
        } else {
            // Weighted Strategy Template
            const topk = bestParams.topk || 50;
            const min_score = bestParams.min_score || 0.0;
            const max_weight = bestParams.max_weight || 1.0;

            return `"""
策略名称: ${name}
策略描述: ${description || '由参数优化实验室自动生成'}
创建时间: ${timestamp}
来源: 自动生成 (Genetic Algorithm)
最佳参数: TopK=${topk}, MinScore=${min_score}, MaxWeight=${max_weight}
"""

STRATEGY_CONFIG = {
    "class": "SimpleWeightStrategy",
    "module_path": "app.utils.recording_strategy",
    "kwargs": {
        "signal": "<PRED>",
        "topk": ${topk},
        "min_score": ${min_score},
        "max_weight": ${max_weight},
    },
}
`;
        }
    };

    const handleSave = async () => {
        if (!name.trim()) {
            setError('请输入策略名称');
            return;
        }

        setIsSaving(true);
        setError('');

        try {
            const code = generateCode();
            const { backtestService } = await import('../../services/backtestService');
            await backtestService.saveStrategy({
                code,
                name,
                description,
                category: 'ai_generated',
                tags: ['auto-generated', strategyType, 'optimized'],
                parameters: bestParams
            });
            setSaveSuccess(true);
            setTimeout(() => {
                onClose();
                setSaveSuccess(false);
                setName('');
                setDescription('');
            }, 1500);
        } catch (err) {
            setError(err instanceof Error ? err.message : '保存失败');
        } finally {
            setIsSaving(false);
        }
    };

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50 backdrop-blur-sm animate-in fade-in duration-200">
            <div className="bg-white rounded-2xl shadow-xl w-full max-w-md overflow-hidden animate-in zoom-in-95 duration-200">

                {/* Header */}
                <div className="px-6 py-4 border-b border-gray-100 flex justify-between items-center bg-gray-50">
                    <div className="flex items-center gap-2 text-gray-800">
                        <Save className="w-5 h-5 text-indigo-600" />
                        <h3 className="font-bold">保存为新策略</h3>
                    </div>
                    <button onClick={onClose} className="text-gray-400 hover:text-gray-600">
                        <X className="w-5 h-5" />
                    </button>
                </div>

                {/* Content */}
                <div className="p-6 space-y-4">
                    {saveSuccess ? (
                        <div className="flex flex-col items-center justify-center py-8 text-green-600 animate-in fade-in duration-300">
                            <CheckCircle className="w-16 h-16 mb-4" />
                            <p className="text-lg font-bold">保存成功！</p>
                        </div>
                    ) : (
                        <>
                            <div>
                                <label className="block text-sm font-medium text-gray-700 mb-1">
                                    策略名称 <span className="text-red-500">*</span>
                                </label>
                                <input
                                    type="text"
                                    value={name}
                                    onChange={(e) => setName(e.target.value)}
                                    placeholder="例如: Topk50_Optimized_v1"
                                    className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 outline-none transition-all"
                                />
                            </div>

                            <div>
                                <label className="block text-sm font-medium text-gray-700 mb-1">
                                    描述 (可选)
                                </label>
                                <textarea
                                    value={description}
                                    onChange={(e) => setDescription(e.target.value)}
                                    placeholder="策略说明..."
                                    className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 outline-none transition-all h-24 resize-none"
                                />
                            </div>

                            <div className="bg-gray-50 rounded-lg p-3 border border-gray-200 text-sm text-gray-600">
                                <div className="flex items-center gap-2 mb-2 font-medium">
                                    <Code className="w-4 h-4" />
                                    将被固化的参数:
                                </div>
                                <div className="space-y-1 pl-6">
                                    {Object.entries(bestParams).map(([key, value]) => (
                                        <div key={key}>
                                            <span className="text-gray-500">{key}:</span> <span className="font-mono text-indigo-700">{value}</span>
                                        </div>
                                    ))}
                                </div>
                            </div>

                            {error && (
                                <div className="text-sm text-red-600 bg-red-50 px-3 py-2 rounded-lg border border-red-100">
                                    {error}
                                </div>
                            )}
                        </>
                    )}
                </div>

                {/* Footer */}
                {!saveSuccess && (
                    <div className="px-6 py-4 border-t border-gray-100 bg-gray-50 flex justify-end gap-3">
                        <button
                            onClick={onClose}
                            className="px-4 py-2 text-gray-600 hover:bg-gray-100 rounded-lg transition-colors font-medium"
                        >
                            取消
                        </button>
                        <button
                            onClick={handleSave}
                            disabled={isSaving}
                            className={`flex items-center gap-2 px-6 py-2 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 transition-all font-medium shadow-sm shadow-indigo-200 ${isSaving ? 'opacity-70 cursor-not-allowed' : ''
                                }`}
                        >
                            {isSaving ? '保存中...' : '确认保存'}
                        </button>
                    </div>
                )}
            </div>
        </div>
    );
};
