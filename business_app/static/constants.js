/* 前端统一领域常量，与后端业务常量保持相同的协议值。 */
window.APS_CONSTANTS = Object.freeze({
  scheduleType: Object.freeze({
    MACHINING: 'machining',
    HEAT_TREATMENT: 'heat_treatment',
    ASSEMBLY: 'assembly',
  }),
  scheduleTypeLabels: Object.freeze({
    machining: '机加',
    heat_treatment: '热表',
    assembly: '装配',
  }),
  scheduleMode: Object.freeze({
    STATIC: 'static',
    DYNAMIC: 'dynamic',
    LOCAL: 'local',
  }),
  scheduleModeLabels: Object.freeze({
    static: '静态全量',
    dynamic: '动态滚动',
    local: '局部微调',
  }),
  deployment: Object.freeze({
    MACHINING: 'MACHINING',
    HEAT_TREATMENT: 'HEAT_TREATMENT',
    ASSEMBLY: 'ASSEMBLY',
    DEBUG: 'DEBUG',
  }),
  deploymentTypes: Object.freeze(['MACHINING', 'HEAT_TREATMENT', 'ASSEMBLY', 'DEBUG']),
  deploymentLabels: Object.freeze({
    MACHINING: '机加',
    HEAT_TREATMENT: '热表',
    ASSEMBLY: '装配',
    DEBUG: '调试',
  }),
  deploymentScheduleTypes: Object.freeze({
    MACHINING: 'machining',
    HEAT_TREATMENT: 'heat_treatment',
    ASSEMBLY: 'assembly',
  }),
  orderBusinessType: Object.freeze({
    MACHINING: 'MACHINING',
    HEAT_TREAT: 'HEAT_TREAT',
    SURFACE_TREAT: 'SURFACE_TREAT',
    ASSEMBLY: 'ASSEMBLY',
  }),
  orderBusinessTypeMapping: Object.freeze({
    MACHINING: 'machining',
    HEAT_TREAT: 'heat_treatment',
    SURFACE_TREAT: 'heat_treatment',
    ASSEMBLY: 'assembly',
  }),
  scheduleOrderBusinessTypes: Object.freeze({
    machining: Object.freeze(['MACHINING']),
    heat_treatment: Object.freeze(['HEAT_TREAT', 'SURFACE_TREAT']),
    assembly: Object.freeze(['ASSEMBLY']),
  }),
  processStatus: Object.freeze({
    PENDING: 'PENDING',
    SCHEDULED: 'SCHEDULED',
    CONFIRMED: 'CONFIRMED',
    IN_PROGRESS: 'IN_PROGRESS',
    PAUSED: 'PAUSED',
    COMPLETED: 'COMPLETED',
    CANCELLED: 'CANCELLED',
  }),
  processStatusLabels: Object.freeze({
    PENDING: '待排程',
    SCHEDULED: '已排程',
    CONFIRMED: '已确认',
    IN_PROGRESS: '执行中',
    PAUSED: '已暂停',
    COMPLETED: '已完成',
    CANCELLED: '已取消',
  }),
  taskStatus: Object.freeze({
    QUEUED: 'QUEUED',
    RUNNING: 'RUNNING',
    SUCCEEDED: 'SUCCEEDED',
    FAILED: 'FAILED',
  }),
  activeTaskStatuses: Object.freeze(['RUNNING', 'QUEUED']),
  versionStatus: Object.freeze({
    DRAFT: 'DRAFT',
    APPROVED: 'APPROVED',
    REJECTED: 'REJECTED',
    PUBLISHED: 'PUBLISHED',
    SUPERSEDED: 'SUPERSEDED',
  }),
  healthStatus: Object.freeze({
    UP: 'UP',
    DOWN: 'DOWN',
  }),
});
