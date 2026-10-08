/* 由 tools/port-from-arena.mjs 生成，勿手改 */
/**
 * 函数式 setState 的类型绕行。
 *
 * 本仓（electron/）的 tsc 环境把 `Dispatch<SetStateAction<S>>` 实例化成 `(value: S) => void`，
 * 函数分支丢失 —— 于是 `setX(prev => ...)` 一律 TS2345，与写法无关，`useState<number>` 同样中招。
 * 这是本仓既有的环境问题（项目代码因此从不用函数式 setter），非 arena 代码有问题。
 *
 * 运行时零影响：React 收到函数就当 updater 处理，`asUpdater(fn)` 原样返回 `fn`。
 * 返回类型 `never` 是为可赋给任何形参 —— 调用点因此不必写任何类型标注。
 */
export function asUpdater<T>(updaterFn: T): never {
  return updaterFn as unknown as never;
}
