/**
 * H5 多用户整改的唯一语义来源。
 *
 * 直接再导出 packages/contracts 生成的 canonical TS 契约（枚举、类型谓词
 * 与 validateX 校验器），不在 H5 内复制第二套漂移枚举。未知枚举值、未知
 * 字段、缺必填、类型不符全部由生成校验器 fail closed。
 *
 * 生成文件是“只使用可擦除 TypeScript 语法”的契约源，Vite/esbuild 可直接
 * 转译；语义与 packages/contracts 的 schema 完全一致，禁止在此文件之外
 * 手写平行规则表。
 */
export * from "@memoria/contracts";
