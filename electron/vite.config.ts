import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Web 部署使用相对路径，通过 Nginx 代理访问后端
// Electron 桌面版使用绝对路径
const apiBase = process.env.VITE_API_URL || process.env.VITE_API_BASE_URL || '';
const wsBase = process.env.VITE_WS_BASE_URL || '';

export default defineConfig(({ mode }) => {
  const getPackageName = (id: string) => {
    const nodeModulesIndex = id.lastIndexOf('/node_modules/');
    if (nodeModulesIndex < 0) return null;

    const modulePath = id.slice(nodeModulesIndex + '/node_modules/'.length);
    const segments = modulePath.split('/');
    if (!segments[0]) return null;

    if (segments[0].startsWith('@') && segments.length >= 2) {
      return `${segments[0]}/${segments[1]}`;
    }

    return segments[0];
  };

  return {
    base: mode === 'production' ? './' : '/',
    plugins: [react()],
    test: {
      globals: true,
      environment: 'jsdom',
      setupFiles: './src/setupTests.ts',
      coverage: {
        reporter: ['text', 'json', 'html'],
        exclude: [
          'node_modules/',
          'src/setupTests.ts',
        ]
      }
    },
    define: {
      'process.env.NODE_ENV': JSON.stringify(mode),
      'process.env.VITE_DEV': JSON.stringify(process.env.VITE_DEV || '0'),
      'process.env.VITE_API_BASE_URL': JSON.stringify(apiBase),
      'process.env.VITE_WS_BASE_URL': JSON.stringify(wsBase),
      'process.env.REACT_APP_API_BASE_URL': JSON.stringify(process.env.REACT_APP_API_BASE_URL || 'http://localhost:8000'),
      // OSS Edition - local storage only
      'process.env.COS_SECRET_ID': JSON.stringify(''),
      'process.env.COS_SECRET_KEY': JSON.stringify(''),
      'process.env.COS_BUCKET': JSON.stringify(''),
      'process.env.COS_REGION': JSON.stringify(''),
      'process.env.TENCENT_SECRET_ID': JSON.stringify(''),
      'process.env.TENCENT_SECRET_KEY': JSON.stringify(''),
      'process.env.TENCENT_BUCKET': JSON.stringify(''),
      'process.env.TENCENT_REGION': JSON.stringify(''),
      'process.env.TENCENT_COS_URL': JSON.stringify(''),
      'process.env.VITE_TENCENT_COS_URL': JSON.stringify(''),
    },
    build: {
      outDir: 'dist-react',
      sourcemap: mode === 'development',
      rollupOptions: {
        input: {
          main: 'index.html'
        },
        output: {
          manualChunks(id) {
            if (!id.includes('node_modules')) {
              return undefined;
            }

            if (id.includes('@monaco-editor') || id.includes('monaco-editor')) return 'vendor-monaco';
            if (id.includes('echarts')) return 'vendor-echarts';
            if (id.includes('framer-motion')) return 'vendor-motion';
            if (id.includes('react-router')) return 'vendor-router';
            if (id.includes('date-fns')) return 'vendor-datefns';
            if (id.includes('prismjs')) return 'vendor-prism';
            if (id.includes('react-markdown') || id.includes('remark-gfm')) return 'vendor-markdown';
            if (id.includes('lucide-react')) return 'vendor-icons';

            const packageName = getPackageName(id);
            if (!packageName) {
              return undefined;
            }

            return `vendor-${packageName.replace('@', '').replace('/', '-')}`;
          },
        },
      }
    },
    server: {
      port: parseInt(process.env.VITE_PORT || '3000'),
      strictPort: true,
      host: '0.0.0.0', // 允许外部访问
      proxy: {
        '/api': {
          target: process.env.VITE_API_URL || 'http://localhost:8000',
          changeOrigin: true,
        },
        '/ws': {
          target: process.env.VITE_WS_URL || 'ws://localhost:8000',
          ws: true,
        },
        '/api/tencent': {
          target: 'https://qt.gtimg.cn',
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/api\/tencent/, ''),
          configure: (proxy, options) => {
            proxy.on('proxyReq', (proxyReq, req, res) => {
              proxyReq.setHeader('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36');
            });
          }
        }
      }
    }
  };
});
