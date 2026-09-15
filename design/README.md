# ProtocolLab — design package v0.1

Đọc `ProtocolLab_Design_Spec_v0.1_vi.html` trong trình duyệt để xem bản đầy đủ có mục lục; Markdown là bản nguồn chỉnh sửa được.

## Nội dung

- `ProtocolLab_Design_Spec_v0.1_vi.md` / `.html`: full design spec tiếng Việt.
- `experiment.example.yaml`: budgets và experiment settings đề xuất. Các model/dependency hash để trống có chủ đích; phải pin trước khi chạy nghiên cứu.
- `contracts/core.schema.json`: JSON Schema draft 2020-12 cho bảy core message types. Type validation không xác nhận authority/truth.
- `contracts/example_action.json`: một ActionProposal đúng schema, không phải authorization.
- `contracts/owner_store.sql`: DDL khởi đầu, chưa có owner services/gateway/reconciliation.
- `fixtures/check_design_fixture.py`: kiểm tra logic của episode minh họa và toy fence.
- `fixtures/fixture_check_result.json`: kết quả chạy các kiểm tra minh họa.

## Kiểm tra fixture

```sh
python fixtures/check_design_fixture.py
```

Chỉ cần Python standard library. Script không có LLM call, không truy cập mạng hoặc tài nguyên deployment thật.

Đã kiểm: hai histories có cùng current public observation nhưng khác output của một suffix; copy-on-write minh họa; stale epoch khi pause; effect đang chạy hoàn tất sau pause; transport dedup khác domain retry; redirect làm plan cũ stale.

**Chưa triển khai/chưa test:** learner AALpy được tích hợp, LLM actor, IPC/OS isolation, cryptographic control channel, durable crash recovery, full benchmark hoặc các giả thuyết performance/alignment. Đây là gói thiết kế, không phải một agent đã chạy end-to-end.

JSON Schema đã được kiểm bằng Draft202012Validator; example action qua schema; DDL đã tạo thành công trên SQLite in-memory; config sizing được kiểm nhất quán. Cross-field semantic validation còn thuộc implementation.

Không có tuyên bố novelty đã được xác nhận bằng systematic literature review. Các ngưỡng và kết quả mong muốn trong spec là proposal, không phải số đo.
