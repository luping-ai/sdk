package localai

import (
	"context"
	"errors"
	"fmt"
	"time"
)

// withConnectTimeout 派生一个只覆盖"建立连接 + 快速响应"的 context。
//
// 用于健康检查这类应当快速返回的请求。若 Runtime 卡死，
// 不该让调用方等 60 秒——1 秒内就该知道"它没响应"。
//
// 若调用方的 ctx 已有更短 deadline，context 语义会自动取更短者，
// 无需额外判断。
func (c *Client) withConnectTimeout(parent context.Context) (context.Context, context.CancelFunc) {
	return context.WithTimeout(parent, c.timeouts.Connect)
}

// withTotalTimeout 派生总超时 context。
//
// 用于推理与检索这类可能较慢的请求。
func (c *Client) withTotalTimeout(parent context.Context) (context.Context, context.CancelFunc) {
	return context.WithTimeout(parent, c.timeouts.Total)
}

// doWithoutRetry 执行不重试的请求。
//
// 健康检查不该重试：它本身就是"探活"，重试会让"Runtime 挂了"
// 这个问题延迟 3 秒才暴露（1s × 3 次），而调用方此时应当立刻
// 得到答案。
func (c *Client) doWithoutRetry(ctx context.Context, method, path string, body any) (*envelope, error) {
	payload, err := marshalBody(body)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrInvalidRequest, err)
	}
	return c.doOnce(ctx, method, path, payload)
}

// asAPIError 是 errors.As 的薄封装，便于在条件语句里用。
func asAPIError(err error, target **APIError) bool {
	return errors.As(err, target)
}

// wrapDecodeErr 包装 JSON 解析失败。
//
// 归为 ErrRuntimeUnavailable 而不是 ErrInvalidRequest：解析失败
// 通常意味着"连到了别的服务"或"版本不兼容"，不是调用方的参数问题。
func wrapDecodeErr(err error) error {
	return fmt.Errorf("%w: 响应格式不符（可能版本不兼容）: %v",
		ErrRuntimeUnavailable, err)
}

// RetryAfterOf 返回错误建议的等待时长（未提供时返回 0）。
//
// 便捷函数：业务方想自己控制重试时用它读服务端的建议。
func RetryAfterOf(err error) time.Duration {
	var apiErr *APIError
	if errors.As(err, &apiErr) {
		return apiErr.RetryAfter
	}
	return 0
}

// IsRetryable 判断该错误是否值得重试。
//
// 与服务端返回的 Retryable 字段一致，同时补上网络层错误的判断。
//
// 注意：SDK 内部已按文档 24.4 自动重试。本函数供需要自己
// 控制重试策略的调用方使用（如想重试更多次或加自定义退避）。
func IsRetryable(err error) bool {
	if err == nil {
		return false
	}
	var apiErr *APIError
	if errors.As(err, &apiErr) {
		return apiErr.Retryable
	}
	// 网络层失败可重试
	return errors.Is(err, ErrRuntimeUnavailable) || errors.Is(err, ErrTimeout)
}
