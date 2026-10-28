import {defineConfig} from 'vite'
import nativeConfig from './vite.config'

export default defineConfig(environment => {
  const original = typeof nativeConfig === 'function' ? nativeConfig(environment) : nativeConfig
  return {
    ...original,
    define: {...original.define, 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('/api')},
    build: {
      ...original.build,
      rollupOptions: {...original.build?.rollupOptions, input: './purpose.html'},
    },
    server: {
      ...original.server,
      host: '127.0.0.1',
      port: 8851,
      strictPort: true,
      allowedHosts: ['127.0.0.1', 'localhost'],
      proxy: {'/api': {target: 'http://127.0.0.1:8850', changeOrigin: false}},
    },
  }
})
