package localai

import "context"

// PressureLevel 是资源压力等级。
//
// 用大写枚举而不是 low/medium/high：与 `/api/v1/resources` 的原始值
// 一致，避免"SDK 一套词汇、服务端另一套"。
type PressureLevel string

const (
	// PressureNormal 内存充足，可以正常使用。
	PressureNormal PressureLevel = "NORMAL"
	// PressureLimited 可用内存偏少。仍可工作，但大模型可能装不下。
	PressureLimited PressureLevel = "LIMITED"
	// PressureBusy 明显紧张。导入解析与推理同时进行容易 OOM。
	PressureBusy PressureLevel = "BUSY"
	// PressureCritical 即将耗尽。应当暂停新任务并提示用户。
	PressureCritical PressureLevel = "CRITICAL"
)

// Consumer 是一个占用内存的其他程序。
//
// Runtime 自身已从列表里排除——把"自己"算进"谁占了内存"会让用户
// 去关掉一个不该关的东西。
type Consumer struct {
	PID  int    `json:"pid"`
	Name string `json:"name"`
	// Bytes 是占用字节数
	Bytes int64 `json:"bytes"`
	// Readable 是人类可读的占用（"1.2 GiB"）。
	//
	// 服务端算好的，不要自己再格式化一遍——两边格式不一致会让用户
	// 以为看到了两个不同的数字。
	Readable string `json:"readable_size"`
}

// ResourceAction 是一条行动建议。
type ResourceAction struct {
	Kind   string `json:"kind"`
	Text   string `json:"text"`
	Detail string `json:"detail,omitempty"`
}

// ResourceAdvice 是给用户的行动建议。
//
// **只在值得提示时出现**（压力等级 ≥ LIMITED）。资源充足时为 nil——
// 一堆"关闭其他程序"的建议纯属打扰。
type ResourceAdvice struct {
	Message string           `json:"message"`
	Actions []ResourceAction `json:"actions,omitempty"`
}

// IdleStatus 是空闲卸载的当前状态。
//
// ## 卸载需要两个条件同时满足
//
//  1. 模型空闲 ≥ UnloadAfterSeconds
//  2. 用户空闲 ≥ UserIdleAfterSeconds
//
// 只有第一条不够：模型空闲（没人调用）不等于用户离开。用户可能正
// 开着文档准备提问，此时卸载会让他在下次点击时等重新加载。
//
// ## 必须展示 UserIdleDetectable 与 LastReason
//
// 用户看到"模型一直不卸载"时会怀疑功能失效。看到
// UserIdleDetectable=false 才能明白是平台不支持，而不是配置错了。
type IdleStatus struct {
	// Enabled 表示自动卸载是否生效（有模型加载且阈值 > 0）
	Enabled bool `json:"enabled"`
	// ModelIdleSeconds 是模型已空闲秒数（距最后一次推理）
	ModelIdleSeconds int `json:"model_idle_seconds"`
	// UnloadAfterSeconds 是模型空闲卸载阈值
	UnloadAfterSeconds int `json:"unload_after_seconds"`
	// UserIdleAfterSeconds 是要求用户空闲的阈值
	UserIdleAfterSeconds int `json:"user_idle_after_seconds"`
	// UserIdleDetectable 表示平台是否支持检测用户活动。
	//
	// **为 false 时自动卸载永不触发**（保守策略：判断不了就当作用户
	// 在忙）。Linux / macOS 当前为 false。
	UserIdleDetectable bool `json:"user_idle_detectable"`
	// UserIdleSeconds 是用户已空闲秒数（检测不到时为 0）
	UserIdleSeconds int `json:"user_idle_seconds"`
	// UserIdleSource 是检测来源说明。Windows 为 GetLastInputInfo；
	// 不支持时给出原因（如 "no graphical session (HEADLESS)"）。
	UserIdleSource string `json:"user_idle_source,omitempty"`
	// LastReason 是最近一次判断的依据，回答"为什么没卸载"。
	//
	// 典型值："model still recently used (15s)"、"user is active (2s)"、
	// "user activity unknown, keeping model loaded"。
	LastReason string `json:"last_reason,omitempty"`
	// UnloadsTotal 是累计因空闲而卸载的次数
	UnloadsTotal int64 `json:"unloads_total"`
}

// ResourceStatus 是当前资源占用状态。
//
// ## 与 Capability 的分工
//
//	Capability   这台机器**能做什么**（启动时确定）
//	ResourceStatus **现在有多紧张**（每次调用重新采样）
//
// 两者不可互相替代：能力是快照，压力是实时值。内存 16 GiB 的机器
// Capability 永远是 full，但用户开了虚拟机之后 Pressure 会到 CRITICAL。
//
// ## 典型用法
//
//	st, err := client.Resources(ctx)
//	if err != nil {
//		// 拿不到状态不是致命错误，退回"撞到 resource_limited 再退避"
//	}
//	if st.Pressure == localai.PressureCritical {
//		pauseScan() // 别撞 OOM
//	}
//
// 这不是"要不要发请求"的开关：Runtime 自己会在准入时拦下放不下的
// 请求并返回 ErrResourceLimited。本方法让你**提前让路**，减少无谓往返
// 与 OOM 风险，不替代那个错误处理。
type ResourceStatus struct {
	// Pressure 是压力等级
	Pressure PressureLevel `json:"pressure"`
	// AvailableBytes / TotalBytes 便于前端展示进度条
	AvailableBytes int64 `json:"available_bytes"`
	TotalBytes     int64 `json:"total_bytes"`
	// AvailablePercent 是可用百分比（0~100）
	AvailablePercent int `json:"available_percent"`
	// Reason 说明为何是该等级
	Reason string `json:"reason,omitempty"`
	// Consumers 是占用内存最多的其他程序（已排除 Runtime 自身）
	Consumers []Consumer `json:"consumers,omitempty"`
	// Advice 是行动建议。仅压力 ≥ LIMITED 时出现。
	Advice *ResourceAdvice `json:"advice,omitempty"`
	// Enforcement 是引擎内存限额的实际状态：
	// enforced / soft_only / disabled
	Enforcement string `json:"enforcement,omitempty"`
	// EnforcementNote 是对限额状态的解释
	EnforcementNote string `json:"enforcement_note,omitempty"`
	// Capability 是这台机器能做什么。
	//
	// nil 表示未评估（未启用推理的部署）。内嵌在这里是为了让状态栏
	// 一次请求就能同时拿到"紧张程度"与"能用什么"。
	Capability *Capability `json:"capability,omitempty"`
	// Idle 是空闲卸载状态。nil 表示未启用推理或未配置。
	Idle *IdleStatus `json:"idle,omitempty"`
}

// Strained 表示是否已到需要让路的程度（LIMITED 及以上）。
//
// 判据与"服务端自己会不会给出警告"一致，而不是另设一个阈值——两边
// 各定一套必然不同步。需要更细的判断（比如只在 BUSY 才暂停扫描）
// 就直接比较 Pressure。
func (r ResourceStatus) Strained() bool {
	switch r.Pressure {
	case PressureLimited, PressureBusy, PressureCritical:
		return true
	default:
		return false
	}
}

// Resources 返回当前的实时资源状态（压力等级、可用内存、占用者）。
//
// ## 与 Capability 不是一回事
//
// Capability 是**启动时算一次的快照**，回答"这台机器能做什么"；
// 本方法每次调用都重新采样，回答"现在有多紧张"。
//
// 内存 16 GiB 的机器 Capability 永远是 full，但用户开了虚拟机、
// 导着大 PDF、同时提问时 Pressure 会到 CRITICAL。所以"资源紧张时
// 暂停扫描"只能用本方法判断。
//
// 查询本身很轻（一次内存采样 + 进程列表），可以按需频繁调用；
// 但**不要放进每篇文档的循环里**——那会让扫描被几百次进程枚举拖慢。
// 建议每批（如 50 篇）或每几秒查一次。
func (c *Client) Resources(ctx context.Context) (*ResourceStatus, error) {
	ctx, cancel := c.withConnectTimeout(ctx)
	defer cancel()

	env, err := c.doWithoutRetry(ctx, "GET", "/api/v1/resources", nil)
	if err != nil {
		return nil, err
	}
	var out ResourceStatus
	if err := unmarshalJSON(env.Data, &out); err != nil {
		return nil, wrapDecodeErr(err)
	}
	return &out, nil
}
