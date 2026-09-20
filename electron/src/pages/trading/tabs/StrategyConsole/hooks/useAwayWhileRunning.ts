import { useEffect, useRef, useState } from 'react';

/**
 * 「策略在跑期间，用户是否离开过本页」（T-RC-20）。
 *
 * 控制台对用户的核心承诺之一是**关闭页面不影响运行**——但承诺本身不可验证，所以
 * 界面要能说「你走开的这段时间它一直在跑，这是它的心跳」。本 hook 只负责记录
 * 「离开过」这一个事实，是否真的还在跑由调用方用心跳证据回答。
 *
 * 三点实现约束：
 * 1. 监听器只挂一次（空依赖），运行态经 ref 取最新值——否则闭包锁死首帧的
 *    `false`，用户启动策略后离开就永远记不到；
 * 2. 只记 `hidden`，不记 `visible`：用户反复切标签页只该提示一次，回来后不再唠叨；
 * 3. 停止后自动复位——运行态转 false 就清空标记。让调用方手动 reset 是错的：
 *    漏调一次，下一轮启动就会顶着一句「你离开时它一直在跑」的假提示。
 */
export function useAwayWhileRunning(isRunning: boolean): boolean {
    const [awayWhileRunning, setAwayWhileRunning] = useState(false);
    const runningRef = useRef(isRunning);
    runningRef.current = isRunning;

    useEffect(() => {
        const onVisibility = () => {
            if (document.visibilityState === 'hidden' && runningRef.current) {
                setAwayWhileRunning(true);
            }
        };
        document.addEventListener('visibilitychange', onVisibility);
        return () => document.removeEventListener('visibilitychange', onVisibility);
    }, []);

    useEffect(() => {
        if (!isRunning) setAwayWhileRunning(false);
    }, [isRunning]);

    return awayWhileRunning;
}
