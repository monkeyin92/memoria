// Labels for the growth overview and digital-self versions. Keys mirror
// services/growth/policy.py and services/digital_self/domain.py.
const DIMENSION_LABELS = Object.freeze({
  life_chapters: "人生阶段",
  important_people: "重要的人",
  expression: "说话方式",
  decision_cases: "做决定的方式",
  relationship_models: "相处方式",
  voice: "声音",
  legacy: "想留下的话",
});

const STATUS_LABELS = Object.freeze({
  empty: "还没有内容",
  emerging: "正在积累",
  supported: "已有依据",
  conflicted: "有内容待你确认",
});

const READINESS_LABELS = Object.freeze({
  ready: "已在当前版本里",
  stale: "当前版本待更新",
  not_built: "还没有生成版本",
  dependency_pending: "暂未开放",
});

const VERSION_STATUS_LABELS = Object.freeze({
  draft: "草稿",
  testing: "评测中",
  approved: "已通过",
  frozen: "已冻结",
  revoked: "已撤回",
});

function sourceCount(dimension) {
  return Array.isArray(dimension?.adopted_sources) ? dimension.adopted_sources.length : 0;
}

function dimensionRows(dimensions) {
  return (Array.isArray(dimensions) ? dimensions : [])
    .filter((item) => DIMENSION_LABELS[item?.key])
    .map((item) => {
      const count = sourceCount(item);
      const readiness = item.version_readiness?.status;
      return {
        key: item.key,
        label: DIMENSION_LABELS[item.key],
        sourceCount: count,
        statusLabel:
          readiness === "dependency_pending" ? READINESS_LABELS.dependency_pending : STATUS_LABELS[item.status] || STATUS_LABELS.empty,
        readinessLabel: count > 0 && readiness !== "dependency_pending" ? READINESS_LABELS[readiness] || "" : "",
        pending: item.status === "conflicted",
      };
    });
}

function versionRows(versions) {
  return (Array.isArray(versions) ? versions : []).map((item, index) => ({
    versionId: typeof item?.version_id === "string" ? item.version_id : `v${index}`,
    number: Number.isInteger(item?.version_number) ? item.version_number : null,
    statusLabel: VERSION_STATUS_LABELS[item?.status] || "处理中",
    manifestLabel: item?.manifest_sha256 ? "已绑定资料摘要" : "资料待整理",
  }));
}

function digitalSelfView({ dimensions, versions, persona } = {}) {
  const rows = dimensionRows(dimensions);
  return {
    dimensionRows: rows,
    versionRows: versionRows(versions),
    sourceTotal: rows.reduce((sum, row) => sum + row.sourceCount, 0),
    supportedCount: rows.filter((row) => row.sourceCount > 0).length,
    dimensionTotal: Object.keys(DIMENSION_LABELS).length,
    enabled: Boolean(persona && persona.enabled),
  };
}

module.exports = { DIMENSION_LABELS, digitalSelfView };
