package localai

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"
)

// 知识库管理的请求/响应编解码。
//
// 这一组的重点是**wire 字段名**：SDK 的结构体与服务端各自手写，
// 字段名写错编译期无感、运行期恒为零值。tools/fieldcheck 保证
// 静态一致，这些测试保证动态（真实 JSON）也一致。
func TestCreateKB(t *testing.T) {
	var got map[string]any
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("method = %s", r.Method)
		}
		if r.URL.Path != "/api/v1/knowledge/bases" {
			t.Errorf("path = %s", r.URL.Path)
		}
		_ = json.NewDecoder(r.Body).Decode(&got)
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write(okEnvelope(map[string]any{
			"id": "kb1", "name": "售后库", "description": "客服问答",
		}))
	})

	ref, err := c.CreateKB(context.Background(), &CreateKBRequest{
		ID: "kb1", Name: "售后库", Description: "客服问答",
	})
	if err != nil {
		t.Fatalf("创建失败: %v", err)
	}
	if ref.ID != "kb1" || ref.Name != "售后库" {
		t.Errorf("响应未解析: %+v", ref)
	}
	// 请求体必须真的带上了这些字段（不是零值）
	if got["name"] != "售后库" || got["id"] != "kb1" {
		t.Errorf("请求体 = %v", got)
	}
}

// name 为空应在本地就失败，不发请求。
func TestCreateKBRequiresName(t *testing.T) {
	called := false
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		called = true
	})
	if _, err := c.CreateKB(context.Background(), &CreateKBRequest{Name: "  "}); err == nil {
		t.Error("空 name 应报错")
	}
	if _, err := c.CreateKB(context.Background(), nil); err == nil {
		t.Error("nil 请求应报错")
	}
	if called {
		t.Error("校验失败时不该发请求")
	}
}

func TestListKB(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write(okEnvelope(map[string]any{
			"count": 1,
			"bases": []any{map[string]any{
				"id": "kb1", "name": "售后库",
				"document_count": 3, "chunk_count": 42,
				"embedded_count": 40, "created_at": 1700000000,
			}},
		}))
	})

	kbs, err := c.ListKB(context.Background())
	if err != nil {
		t.Fatalf("列表失败: %v", err)
	}
	if len(kbs) != 1 {
		t.Fatalf("数量 = %d", len(kbs))
	}
	kb := kbs[0]
	if kb.ChunkCount != 42 || kb.EmbeddedCount != 40 || kb.DocumentCount != 3 {
		t.Errorf("统计字段未解析: %+v", kb)
	}
	if kb.CreatedAt == 0 {
		t.Error("created_at 丢失")
	}
}

func TestKBStat(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/knowledge/bases/kb1/stats" {
			t.Errorf("path = %s", r.URL.Path)
		}
		_, _ = w.Write(okEnvelope(map[string]any{
			"kb_id": "kb1", "document_count": 3, "chunk_count": 42,
			"embedded_count": 40, "stale_count": 2,
			"vectorize": map[string]any{
				"total_chunks": 42, "embedded_chunks": 40,
				"pending_chunks": 2, "stale_chunks": 0,
				"model": "e5-small", "running": true,
			},
		}))
	})

	st, err := c.KBStat(context.Background(), "kb1")
	if err != nil {
		t.Fatalf("统计失败: %v", err)
	}
	if st.StaleCount != 2 {
		t.Errorf("StaleCount = %d", st.StaleCount)
	}
	if st.Vectorize == nil {
		t.Fatal("vectorize 应为非 nil")
	}
	if st.Vectorize.Pending != 2 || !st.Vectorize.Running {
		t.Errorf("向量化进度未解析: %+v", st.Vectorize)
	}
	if st.Vectorize.Model != "e5-small" {
		t.Errorf("model = %q", st.Vectorize.Model)
	}
}

// vectorize 可能缺省（服务端查进度失败时不影响主统计），
// 不能让整个解析失败。
func TestKBStatWithoutVectorize(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write(okEnvelope(map[string]any{
			"kb_id": "kb1", "chunk_count": 5,
		}))
	})

	st, err := c.KBStat(context.Background(), "kb1")
	if err != nil {
		t.Fatalf("统计失败: %v", err)
	}
	if st.ChunkCount != 5 {
		t.Errorf("ChunkCount = %d", st.ChunkCount)
	}
	if st.Vectorize != nil {
		t.Errorf("vectorize 应为 nil, got %+v", st.Vectorize)
	}
}

// 同步模式下 vectorized 为 0 是有效答案（没有待处理的切片），
// 不能被 omitempty 吞掉——否则调用方无法与异步模式区分。
func TestVectorizeSyncZero(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write(okEnvelope(map[string]any{
			"kb_id": "kb1", "vectorized": 0,
		}))
	})

	res, err := c.Vectorize(context.Background(), "kb1", nil)
	if err != nil {
		t.Fatalf("向量化失败: %v", err)
	}
	if res.Started {
		t.Error("同步模式 Starttarted 应为 false")
	}
	if res.Vectorized != 0 {
		t.Errorf("Vectorized = %d", res.Vectorized)
	}
}

func TestVectorizeAsync(t *testing.T) {
	var got map[string]any
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&got)
		_, _ = w.Write(okEnvelope(map[string]any{
			"kb_id": "kb1", "started": true,
			"note": "向量化已在后台开始，可通过 /stats 查看进度",
		}))
	})

	res, err := c.Vectorize(context.Background(), "kb1", &VectorizeRequest{Async: true})
	if err != nil {
		t.Fatalf("向量化失败: %v", err)
	}
	if !res.Started || res.Note == "" {
		t.Errorf("异步响应未解析: %+v", res)
	}
	if got["async"] != true {
		t.Errorf("请求体未带上 async: %v", got)
	}
}

// 未配置 Embedding 模型是可判定的错误，不是"内部错误"。
func TestVectorizeNoModel(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = w.Write(errEnvelope("model_unavailable",
			"未配置 Embedding 模型，只能做关键词检索", false, 0))
	})

	_, err := c.Vectorize(context.Background(), "kb1", nil)
	if !errors.Is(err, ErrModelUnavailable) {
		t.Errorf("应归为 ErrModelUnavailable, got %v", err)
	}
}

func TestListDocuments(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write(okEnvelope(map[string]any{
			"count": 1,
			"documents": []any{map[string]any{
				"id": "d1", "name": "手册.pdf", "file_type": "pdf",
				"storage_mode": "MANAGED", "file_size": 10240,
				"status": "ACTIVE", "index_status": "READY",
				"chunk_count": 12,
				"created_at":  1700000000, "updated_at": 1700000001,
			}},
		}))
	})

	docs, err := c.ListDocuments(context.Background(), "kb1")
	if err != nil {
		t.Fatalf("列表失败: %v", err)
	}
	if len(docs) != 1 {
		t.Fatalf("数量 = %d", len(docs))
	}
	d := docs[0]
	if d.Status != "ACTIVE" || d.IndexStatus != "READY" {
		t.Errorf("状态未解析: %+v", d)
	}
	if d.FileSize != 10240 || d.ChunkCount != 12 {
		t.Errorf("数值字段未解析: %+v", d)
	}
}

func TestImportSuccess(t *testing.T) {
	var got map[string]any
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/knowledge/bases/kb1/documents" {
			t.Errorf("path = %s", r.URL.Path)
		}
		_ = json.NewDecoder(r.Body).Decode(&got)
		_, _ = w.Write(okEnvelope(map[string]any{
			"document_id": "d1", "chunk_count": 12,
			"content_runes": 3400, "duplicate": false,
		}))
	})

	res, err := c.Import(context.Background(), "kb1", &ImportRequest{
		Path: `D:\docs\手册.pdf`, Password: "secret",
	})
	if err != nil {
		t.Fatalf("导入失败: %v", err)
	}
	if res.ChunkCount != 12 || res.ContentRunes != 3400 {
		t.Errorf("响应未解析: %+v", res)
	}
	if res.Duplicate {
		t.Error("Duplicate 应为 false")
	}
	if got["password"] != "secret" {
		t.Errorf("密码未透传: %v", got)
	}
}

// 重复导入不是错误，而是 Duplicate=true + 已有文档 ID。
func TestImportDuplicate(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write(okEnvelope(map[string]any{
			"document_id": "d2", "duplicate": true,
			"existing_id": "d1",
			"note":        "该内容已存在于知识库中，未重复导入",
		}))
	})

	res, err := c.Import(context.Background(), "kb1", &ImportRequest{Path: "/a.txt"})
	if err != nil {
		t.Fatalf("重复导入不该报错: %v", err)
	}
	if !res.Duplicate || res.ExistingID != "d1" {
		t.Errorf("去重信息未解析: %+v", res)
	}
}

// 增量更新返回 diff，其中 needs_embedding 决定是否要触发向量化。
func TestImportUpdateWithDiff(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write(okEnvelope(map[string]any{
			"document_id": "d1", "chunk_count": 12,
			"diff": map[string]any{
				"added": 1, "updated": 2, "deleted": 0, "unchanged": 9,
				"needs_embedding": true, "summary": "变更 3，未变 9",
			},
		}))
	})

	res, err := c.Import(context.Background(), "kb1", &ImportRequest{
		Path: "/a.txt", UpdateDocumentID: "d1",
	})
	if err != nil {
		t.Fatalf("更新失败: %v", err)
	}
	if res.Diff == nil {
		t.Fatal("diff 应为非 nil")
	}
	if res.Diff.Added != 1 || res.Diff.Updated != 2 || res.Diff.Unchanged != 9 {
		t.Errorf("diff 未解析: %+v", res.Diff)
	}
	if !res.Diff.NeedsEmbedding {
		t.Error("needs_embedding 丢失——调用方会漏掉向量化")
	}
}

func TestImportValidation(t *testing.T) {
	called := false
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		called = true
	})
	if _, err := c.Import(context.Background(), "", &ImportRequest{Path: "/a.txt"}); err == nil {
		t.Error("空 kb_id 应报错")
	}
	if _, err := c.Import(context.Background(), "kb1", &ImportRequest{}); err == nil {
		t.Error("空 path 应报错")
	}
	if _, err := c.Import(context.Background(), "kb1", nil); err == nil {
		t.Error("nil 请求应报错")
	}
	if called {
		t.Error("校验失败时不该发请求")
	}
}

// 加密 PDF 缺密码是可解决的：服务端标了 retryable，SDK 应透传。
func TestImportPasswordRequired(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write(errEnvelope("invalid_request",
			"PDF 已加密，需要密码", true, 0))
	})

	_, err := c.Import(context.Background(), "kb1", &ImportRequest{Path: "/a.pdf"})
	if !errors.Is(err, ErrInvalidRequest) {
		t.Fatalf("应归为 ErrInvalidRequest, got %v", err)
	}
	if !IsRetryable(err) {
		t.Error("补密码后重试有意义，应标记为可重试")
	}
}

func TestDeleteDocument(t *testing.T) {
	var gotPath, gotQuery string
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		gotPath, gotQuery = r.URL.Path, r.URL.RawQuery
		_, _ = w.Write(okEnvelope(map[string]any{
			"deleted": "d1", "file_removed": true,
		}))
	})

	if err := c.DeleteDocument(context.Background(), "kb1", "d1", false); err != nil {
		t.Fatalf("删除失败: %v", err)
	}
	if gotPath != "/api/v1/knowledge/bases/kb1/documents/d1" {
		t.Errorf("path = %s", gotPath)
	}
	if gotQuery != "" {
		t.Errorf("keepFile=false 时不该带 keep_file: %q", gotQuery)
	}

	if err := c.DeleteDocument(context.Background(), "kb1", "d1", true); err != nil {
		t.Fatalf("删除失败: %v", err)
	}
	if !strings.Contains(gotQuery, "keep_file=1") {
		t.Errorf("keepFile=true 应带 keep_file=1, got %q", gotQuery)
	}
}

// ID 含特殊字符时必须转义，否则会拼出错误路径甚至路径穿越。
func TestKnowledgeIDPathEscape(t *testing.T) {
	var gotPath string
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		// 用 EscapedPath 看原始转义形式
		gotPath = r.URL.EscapedPath()
		_, _ = w.Write(okEnvelope(map[string]any{}))
	})

	_ = c.DeleteKB(context.Background(), "kb/../evil")
	if !strings.Contains(gotPath, "kb%2F..%2Fevil") {
		t.Errorf("ID 未转义: %s", gotPath)
	}
}

func TestDeleteKB(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodDelete {
			t.Errorf("method = %s", r.Method)
		}
		_, _ = w.Write(okEnvelope(map[string]any{"deleted": "kb1"}))
	})

	if err := c.DeleteKB(context.Background(), "kb1"); err != nil {
		t.Fatalf("删除失败: %v", err)
	}
	if err := c.DeleteKB(context.Background(), ""); err == nil {
		t.Error("空 kb_id 应报错")
	}
}

// 资源不存在应归为 ErrNotFound，而不是 ErrInvalidRequest。
//
// 这不是吹毛求疵：两者给用户的提示完全不同——前者是"找不到"，
// 后者是"参数写错了"。开发中知识库不存在返回的是 model_not_found，
// SDK 把它当成参数错误，业务方查了半天参数。
func TestResourceNotFound(t *testing.T) {
	for _, code := range []string{"not_found", "model_not_found"} {
		c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusNotFound)
			_, _ = w.Write(errEnvelope(code, "知识库不存在: kb1", false, 0))
		})

		_, err := c.KBStat(context.Background(), "kb1")
		if !errors.Is(err, ErrNotFound) {
			t.Errorf("%s 应归为 ErrNotFound, got %v", code, err)
		}
		if errors.Is(err, ErrInvalidRequest) {
			t.Errorf("%s 不该被当成参数错误", code)
		}
	}
}

// ---------------------------------------------------------------------------
// 上传导入（multipart）
// ---------------------------------------------------------------------------

// 上传必须真的发出 multipart，且文件名带扩展名。
//
// 服务端按扩展名选解析器——如果 SDK 把文件名丢了，上传会得到
// "不支持的文件类型 "（空扩展名），而调用方完全无法从自己的代码里
// 看出问题。
func TestUploadDocumentSendsMultipart(t *testing.T) {
	var (
		gotFilename string
		gotFields   map[string]string
		gotContent  string
	)

	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/knowledge/bases/kb1/documents/upload" {
			t.Errorf("path = %s", r.URL.Path)
		}
		if !strings.HasPrefix(r.Header.Get("Content-Type"), "multipart/form-data") {
			t.Errorf("Content-Type = %q，应为 multipart",
				r.Header.Get("Content-Type"))
		}

		// 用服务端同款解析器读回来：这样连 boundary 拼错也能抓到
		if err := r.ParseMultipartForm(1 << 20); err != nil {
			t.Fatalf("解析 multipart: %v", err)
		}
		gotFields = map[string]string{}
		for k := range r.MultipartForm.Value {
			gotFields[k] = r.MultipartForm.Value[k][0]
		}

		file, hdr, err := r.FormFile("file")
		if err != nil {
			t.Fatalf("缺少 file part: %v", err)
		}
		defer file.Close()
		gotFilename = hdr.Filename
		buf := make([]byte, 64)
		n, _ := file.Read(buf)
		gotContent = string(buf[:n])

		_, _ = w.Write(okEnvelope(map[string]any{
			"document_id": "d1", "chunk_count": 3,
			"content_runes": 40, "duplicate": false,
		}))
	})

	res, err := c.UploadDocument(context.Background(), "kb1", &UploadRequest{
		File:     strings.NewReader("保修期为两年。"),
		Filename: "保修说明.md",
		Password: "pw",
	})
	if err != nil {
		t.Fatalf("上传失败: %v", err)
	}
	if res.ChunkCount != 3 {
		t.Errorf("响应未解析: %+v", res)
	}

	// 文件名必须原样带扩展名
	if gotFilename != "保修说明.md" {
		t.Errorf("filename = %q, want 保修说明.md", gotFilename)
	}
	if gotContent != "保修期为两年。" {
		t.Errorf("内容 = %q", gotContent)
	}
	if gotFields["password"] != "pw" {
		t.Errorf("password = %q", gotFields["password"])
	}
}

// Filename 缺失必须在客户端就被拒绝。
//
// 放过去的话，服务端会用一个没有扩展名的临时文件导入，得到
// "不支持的文件类型" —— 调用方看到的是一个与服务端内部实现
// 有关的错误，而不是"你漏了 Filename"。
func TestUploadDocumentRequiresFilename(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		t.Error("不该发出请求")
	})

	_, err := c.UploadDocument(context.Background(), "kb1", &UploadRequest{
		File: strings.NewReader("x"),
	})
	if !errors.Is(err, ErrInvalidRequest) {
		t.Errorf("err = %v, want ErrInvalidRequest", err)
	}
}

// File 缺失同样在客户端拒绝。
func TestUploadDocumentRequiresFile(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		t.Error("不该发出请求")
	})

	_, err := c.UploadDocument(context.Background(), "kb1", &UploadRequest{
		Filename: "a.md",
	})
	if !errors.Is(err, ErrInvalidRequest) {
		t.Errorf("err = %v, want ErrInvalidRequest", err)
	}
}

// 空 kb_id 在客户端拒绝。
func TestUploadDocumentRequiresKBID(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		t.Error("不该发出请求")
	})

	_, err := c.UploadDocument(context.Background(), "", &UploadRequest{
		File: strings.NewReader("x"), Filename: "a.md",
	})
	if !errors.Is(err, ErrInvalidRequest) {
		t.Errorf("err = %v, want ErrInvalidRequest", err)
	}
}

// 服务端的结构化错误必须被翻译成可判定的哨兵错误。
//
// 上传是"用户操作"，业务方要据此给出可操作的提示：
// 文件太大 → 换文件；不支持类型 → 换格式。归成笼统的错误
// 就只能显示一句服务端原文。
func TestUploadDocumentTranslatesErrors(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write(errEnvelope("invalid_request",
			"不支持的文件类型 .exe", false, 0))
	})

	_, err := c.UploadDocument(context.Background(), "kb1", &UploadRequest{
		File: strings.NewReader("MZ"), Filename: "a.exe",
	})
	if !errors.Is(err, ErrInvalidRequest) {
		t.Errorf("err = %v, want ErrInvalidRequest", err)
	}
	var apiErr *APIError
	if errors.As(err, &apiErr) {
		if !strings.Contains(apiErr.Message, "不支持的文件类型") {
			t.Errorf("服务端文案丢失: %q", apiErr.Message)
		}
	}
}

// ---------------------------------------------------------------------------
// 入库已切好的块（IngestChunks）
// ---------------------------------------------------------------------------

// 请求必须打到新端点，且字段名与契约一致。
func TestIngestChunksSendsContractFields(t *testing.T) {
	var got map[string]any
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/knowledge/bases/kb1/documents/chunks" {
			t.Errorf("path = %s", r.URL.Path)
		}
		_ = json.NewDecoder(r.Body).Decode(&got)
		_, _ = w.Write(okEnvelope(map[string]any{
			"document_id": "d1", "chunk_count": 2,
			"content_runes": 120, "duplicate": false,
		}))
	})

	res, err := c.IngestChunks(context.Background(), "kb1", &IngestChunksRequest{
		Name: "售后手册", FileType: "pdf", FileHash: "h1",
		FileSize: 1234, ModifiedAt: 1763000000,
		SourceLabel: `D:/工作/售后手册.pdf`,
		Chunker: ChunkerInfo{
			Name: "localkb-py", Version: "1.0.0",
			TargetChars: 450, MaxChars: 600, OverlapChars: 54,
		},
		Chunks: []ChunkInput{
			{Ordinal: 0, Text: "本条款适用于……", PageStart: 3, PageEnd: 3,
				StartRune: 1200, RuneCount: 480,
				HeadingPath: "第三章 > 3.2", ChunkType: "heading"},
			{Ordinal: 1, Text: "保修期为两年。", PageStart: 4},
		},
	})
	if err != nil {
		t.Fatalf("入库失败: %v", err)
	}
	if res.ChunkCount != 2 {
		t.Errorf("响应未解析: %+v", res)
	}

	// 逐个核对字段名：JSON tag 写错不会编译失败，只会在运行时静默丢失
	if got["name"] != "售后手册" || got["file_hash"] != "h1" {
		t.Errorf("基础字段: %v", got)
	}
	if got["file_type"] != "pdf" {
		t.Errorf("file_type = %v", got["file_type"])
	}
	if got["source_label"] != `D:/工作/售后手册.pdf` {
		t.Errorf("source_label 未透传: %v", got["source_label"])
	}

	chunker, _ := got["chunker"].(map[string]any)
	if chunker["name"] != "localkb-py" || chunker["version"] != "1.0.0" {
		t.Errorf("chunker: %v", chunker)
	}
	if chunker["target_chars"] != float64(450) {
		t.Errorf("chunker.target_chars = %v", chunker["target_chars"])
	}

	chunks, _ := got["chunks"].([]any)
	if len(chunks) != 2 {
		t.Fatalf("chunks 数 = %d", len(chunks))
	}
	first, _ := chunks[0].(map[string]any)
	if first["page_start"] != float64(3) || first["page_end"] != float64(3) {
		t.Errorf("页码未透传: %v", first)
	}
	if first["heading_path"] != "第三章 > 3.2" || first["chunk_type"] != "heading" {
		t.Errorf("溯源字段未透传: %v", first)
	}
	if first["start_rune"] != float64(1200) || first["rune_count"] != float64(480) {
		t.Errorf("位置字段未透传: %v", first)
	}
}

// 本地预检：明显错误的请求不该浪费一次往返。
func TestIngestChunksValidation(t *testing.T) {
	called := false
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		called = true
		_, _ = w.Write(okEnvelope(map[string]any{}))
	})

	cases := []struct {
		name string
		kbID string
		req  *IngestChunksRequest
	}{
		{"空 kb_id", "", &IngestChunksRequest{
			Name: "a", FileHash: "h", Chunker: ChunkerInfo{Name: "c", Version: "1"},
			Chunks: []ChunkInput{{Ordinal: 0, Text: "x"}}}},
		{"空 req", "kb1", nil},
		{"空 name", "kb1", &IngestChunksRequest{
			FileHash: "h", Chunker: ChunkerInfo{Name: "c", Version: "1"},
			Chunks: []ChunkInput{{Ordinal: 0, Text: "x"}}}},
		{"空 file_hash", "kb1", &IngestChunksRequest{
			Name: "a", Chunker: ChunkerInfo{Name: "c", Version: "1"},
			Chunks: []ChunkInput{{Ordinal: 0, Text: "x"}}}},
		{"空 chunker.version", "kb1", &IngestChunksRequest{
			Name: "a", FileHash: "h", Chunker: ChunkerInfo{Name: "c"},
			Chunks: []ChunkInput{{Ordinal: 0, Text: "x"}}}},
		{"空 chunks", "kb1", &IngestChunksRequest{
			Name: "a", FileHash: "h", Chunker: ChunkerInfo{Name: "c", Version: "1"}}},
	}
	for _, tc := range cases {
		_, err := c.IngestChunks(context.Background(), tc.kbID, tc.req)
		if !errors.Is(err, ErrInvalidRequest) {
			t.Errorf("%s: err = %v, want ErrInvalidRequest", tc.name, err)
		}
	}
	if called {
		t.Error("本地预检失败的请求不应发出")
	}
}

// 409 chunker_mismatch 必须映射到专用哨兵，且**不可重试**。
func TestIngestChunksChunkerMismatch(t *testing.T) {
	attempts := 0
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		attempts++
		w.WriteHeader(http.StatusConflict)
		_, _ = w.Write(errEnvelope("chunker_mismatch",
			"库内为 localkb-py@1.0.0，本次为 2.0.0，需要重建索引", false, 0))
	})

	_, err := c.IngestChunks(context.Background(), "kb1", &IngestChunksRequest{
		Name: "a", FileHash: "h",
		Chunker: ChunkerInfo{Name: "c", Version: "2.0.0"},
		Chunks:  []ChunkInput{{Ordinal: 0, Text: "x"}},
	})

	if !errors.Is(err, ErrChunkerMismatch) {
		t.Fatalf("err = %v, want ErrChunkerMismatch", err)
	}
	// 这是最关键的一条：重试必然再失败，只会浪费用户时间
	if IsRetryable(err) {
		t.Error("chunker_mismatch 不可重试")
	}
	if attempts != 1 {
		t.Errorf("应只尝试 1 次，实际 %d 次（SDK 不应自动重试）", attempts)
	}
}

// ---------------------------------------------------------------------------
// 相似检测（Phase 2）
// ---------------------------------------------------------------------------

func TestDedupCheckFastPath(t *testing.T) {
	var gotPath string
	var gotBody map[string]any
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &gotBody)
		_, _ = w.Write(okEnvelope(map[string]any{
			"groups": []any{
				map[string]any{
					"id": "dg1", "kind": "similar", "max_hamming": 3,
					"members": []any{
						map[string]any{"document_id": "a", "name": "a.pdf",
							"hamming": 0},
						map[string]any{"document_id": "b", "name": "b.pdf",
							"hamming": 3},
					},
				},
			},
			"pending":  []string{"c"},
			"total_ms": 25,
		}))
	})

	res, err := c.DedupCheck(context.Background(), "kb1", &DedupCheckRequest{
		DocIDs: []string{"a", "b", "c"},
	})
	if err != nil {
		t.Fatalf("检测失败: %v", err)
	}

	if gotPath != "/api/v1/knowledge/bases/kb1/dedup/check" {
		t.Errorf("path = %s", gotPath)
	}
	if ids, _ := gotBody["doc_ids"].([]any); len(ids) != 3 {
		t.Errorf("doc_ids 未透传: %v", gotBody["doc_ids"])
	}

	if len(res.Groups) != 1 {
		t.Fatalf("groups = %d", len(res.Groups))
	}
	g := res.Groups[0]
	if g.Kind != "similar" || g.MaxHamming != 3 {
		t.Errorf("组信息不对: %+v", g)
	}
	if len(g.Members) != 2 {
		t.Fatalf("members = %d", len(g.Members))
	}
	// 第一个必须是代表（Hamming = 0）
	if g.Members[0].DocumentID != "a" || g.Members[0].Hamming != 0 {
		t.Errorf("代表信息不对: %+v", g.Members[0])
	}
	if g.Members[1].Name != "b.pdf" {
		t.Errorf("成员名 = %q", g.Members[1].Name)
	}

	// pending 必须能读到——不检查它就会把"还没算指纹"误读成"没有重复"
	if len(res.Pending) != 1 || res.Pending[0] != "c" {
		t.Errorf("Pending = %v", res.Pending)
	}
	if res.TotalMs != 25 {
		t.Errorf("TotalMs = %d", res.TotalMs)
	}
}

// nil 请求应等价于"检查全库"。
func TestDedupCheckNilRequest(t *testing.T) {
	var gotBody map[string]any
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &gotBody)
		_, _ = w.Write(okEnvelope(map[string]any{"groups": []any{}}))
	})

	if _, err := c.DedupCheck(context.Background(), "kb1", nil); err != nil {
		t.Fatalf("nil 请求应被接受: %v", err)
	}
	if len(gotBody) != 0 {
		t.Errorf("nil 请求不该带字段: %v", gotBody)
	}
}

func TestDedupCheckValidation(t *testing.T) {
	called := false
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		called = true
		_, _ = w.Write(okEnvelope(map[string]any{}))
	})

	_, err := c.DedupCheck(context.Background(), "", nil)
	if !errors.Is(err, ErrInvalidRequest) {
		t.Errorf("空 kb_id 应报 ErrInvalidRequest，实际 %v", err)
	}
	if called {
		t.Error("本地校验失败不该发请求")
	}
}

func TestDedupGroups(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			t.Errorf("method = %s", r.Method)
		}
		_, _ = w.Write(okEnvelope(map[string]any{
			"groups": []any{
				map[string]any{
					"id": "dg1", "kind": "exact", "max_hamming": 0,
					"members": []any{
						map[string]any{"document_id": "a", "hamming": 0},
						map[string]any{"document_id": "b", "hamming": 0},
					},
				},
			},
			"computed_at":   1763000000,
			"stale":         true,
			"pending_count": 4,
		}))
	})

	res, err := c.DedupGroups(context.Background(), "kb1")
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Groups) != 1 || res.Groups[0].Kind != "exact" {
		t.Errorf("groups = %+v", res.Groups)
	}
	if res.ComputedAt != 1763000000 {
		t.Errorf("ComputedAt = %d", res.ComputedAt)
	}
	// stale 是"结果不完整"的信号，必须能读到
	if !res.Stale || res.PendingCount != 4 {
		t.Errorf("stale=%v pending=%d", res.Stale, res.PendingCount)
	}
}

func TestDedupDiff(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		// 查询参数必须正确转义
		q := r.URL.Query()
		if q.Get("doc_a") != "a" || q.Get("doc_b") != "b" {
			t.Errorf("query = %v", q)
		}
		_, _ = w.Write(okEnvelope(map[string]any{
			"doc_a": "a", "doc_b": "b",
			"added": []any{
				map[string]any{"ordinal": 5, "preview": "新增"},
			},
			"removed": []any{
				map[string]any{"ordinal": 2, "preview": "删除"},
			},
			"changed": []any{
				map[string]any{"ordinal": 1, "before": "旧", "after": "新"},
			},
			"unchanged": 7,
			"truncated": true,
		}))
	})

	d, err := c.DedupDiff(context.Background(), "kb1", "a", "b")
	if err != nil {
		t.Fatal(err)
	}
	if len(d.Added) != 1 || d.Added[0].Ordinal != 5 || d.Added[0].Preview != "新增" {
		t.Errorf("Added = %+v", d.Added)
	}
	if len(d.Removed) != 1 || d.Removed[0].Ordinal != 2 {
		t.Errorf("Removed = %+v", d.Removed)
	}
	if len(d.Changed) != 1 || d.Changed[0].Before != "旧" || d.Changed[0].After != "新" {
		t.Errorf("Changed = %+v", d.Changed)
	}
	if d.Unchanged != 7 || !d.Truncated {
		t.Errorf("Unchanged=%d Truncated=%v", d.Unchanged, d.Truncated)
	}
}

func TestDedupDiffValidation(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		t.Error("校验失败不该发请求")
	})

	cases := []struct{ kb, a, b string }{
		{"", "a", "b"},
		{"kb1", "", "b"},
		{"kb1", "a", ""},
		{"kb1", "a", "a"}, // 自己比自己没有信息量
	}
	for _, tc := range cases {
		if _, err := c.DedupDiff(context.Background(), tc.kb, tc.a, tc.b); !errors.Is(err, ErrInvalidRequest) {
			t.Errorf("(%q,%q,%q) 应报 ErrInvalidRequest，实际 %v",
				tc.kb, tc.a, tc.b, err)
		}
	}
}

// DupGroup.Summary 用于日志与 UI，格式必须稳定可读。
func TestDupGroupSummary(t *testing.T) {
	g := DupGroup{
		Kind: "similar", MaxHamming: 3,
		Members: []DupMember{
			{DocumentID: "a", Name: "合同.pdf"},
			{DocumentID: "b", Name: "合同(1).pdf"},
		},
	}
	s := g.Summary()
	if !strings.Contains(s, "similar") || !strings.Contains(s, "合同.pdf") ||
		!strings.Contains(s, "2 份") {
		t.Errorf("Summary = %q", s)
	}
	// 没有 Name 时退回 DocumentID
	g2 := DupGroup{Kind: "exact", Members: []DupMember{{DocumentID: "x"}}}
	if !strings.Contains(g2.Summary(), "x") {
		t.Errorf("无名称时应显示 ID: %q", g2.Summary())
	}
}
