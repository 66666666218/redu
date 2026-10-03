// 前端静态检查(2026-10-03 加)。
//
// **为什么只开 essential + 少量核心规则、不开 recommended**:
// 这个项目此前**完全没有前端 lint**(见 doc/operations.md),而踩过的坑不是"风格不统一",
// 是**真会让页面崩的**:模板里用了未声明的变量 → 整页白屏(改完构建还照样通过)。
// 所以这里只留"会出错"的那一档:未声明变量/组件、非法指令、重复键……
// **格式/风格类一律不开** —— 那会把真问题淹在几百条告警里,结果就是没人看。
//
// 跑法:cd frontend && npx eslint src
// 也被 tests/test_frontend_lint.py 接进测试套件(提交前就会拦住)。
import pluginVue from 'eslint-plugin-vue'

const BROWSER_GLOBALS = {
  window: 'readonly', document: 'readonly', location: 'readonly', navigator: 'readonly',
  localStorage: 'readonly', sessionStorage: 'readonly', console: 'readonly',
  fetch: 'readonly', setTimeout: 'readonly', clearTimeout: 'readonly',
  setInterval: 'readonly', clearInterval: 'readonly', URLSearchParams: 'readonly',
  URL: 'readonly', Blob: 'readonly', FormData: 'readonly', alert: 'readonly',
  confirm: 'readonly', prompt: 'readonly',
}

export default [
  ...pluginVue.configs['flat/essential'],
  {
    files: ['**/*.{js,vue}'],
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: 'module',
      globals: BROWSER_GLOBALS,
    },
    rules: {
      // 核心中的核心:模板/脚本里用了没声明的东西 —— 这就是"整页白屏"的来源
      'no-undef': 'error',
      // ⚠️ `router-link`/`router-view` 由 vue-router **全局注册**,不 import —— 得告诉 lint,
      // 否则每个用到它们的页面都误报(实测:改完第一版就报了 3 个页面)。
      'vue/no-undef-components': ['error', { ignorePatterns: ['^(router-link|router-view)$'] }],
      'vue/no-undef-properties': 'error',
      // 明显写错/写了没用,都会累积成误导
      'vue/no-unused-vars': 'warn',
      'vue/no-dupe-keys': 'error',
      'no-dupe-keys': 'error',
      'no-unreachable': 'error',
      'no-const-assign': 'error',
      // ⚠️ **关掉纯命名约定**:`Platform.vue`/`Screen.vue` 这类单名组件不是 bug ——
      // 开着它只会让 23 个视图全红,真问题被淹掉(这正是"开了 lint 但没人看"的成因)。
      'vue/multi-word-component-names': 'off',
    },
  },
]
