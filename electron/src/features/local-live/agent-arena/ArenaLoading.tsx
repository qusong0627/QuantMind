/**
 * arena 页面的懒加载占位。
 *
 * 页面按栏拆成独立 chunk（@visx / lightweight-charts 只有对应的栏才下载），
 * 首次点开某一栏时用这个占位，视觉上与交易台其它面板一致（灰底 + 转圈）。
 */
const ArenaLoading = ({ label }: { label: string }) => (
  <div className="qm-arena-root flex h-full w-full items-center justify-center bg-white">
    <div className="flex flex-col items-center gap-3 text-gray-500">
      <div className="h-6 w-6 animate-spin rounded-full border-2 border-gray-300 border-t-gray-600" />
      <span className="text-sm">{label}加载中…</span>
    </div>
  </div>
);

export default ArenaLoading;
