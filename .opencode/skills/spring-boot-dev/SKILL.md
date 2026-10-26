---
name: spring-boot-dev
description: Use when designing, implementing, refactoring, or testing Java/Kotlin Spring Boot services with Maven/Gradle and JUnit 5.
---

# Spring Boot 企業研發與測試規範

## 1. 分層與實作慣例
- **遵循既有專案風格**：新增類別前先觀察同專案現有的套件結構、統一回應封裝（如 `ApiResponse<T>`）、全域例外處理（`@RestControllerAdvice`）與日誌規範（SLF4J）。
- **建構子注入**：一律使用建構子依賴注入（或專案既有的 Lombok `@RequiredArgsConstructor`），避免使用 `@Autowired` 欄位注入。
- **交易與資料存取安全**：
  - 寫入操作明確標註 `@Transactional`，唯讀查詢標註 `@Transactional(readOnly = true)`。
  - 避免在同一個類別內直接自呼叫 `@Transactional` 方法導致 AOP 代理失效。
  - 嚴禁在 `@Transactional` 交易區塊內執行耗時的外部 HTTP/RPC 呼叫。
  - 嚴禁拼接動態 SQL 字串，一律使用參數化查詢（JPA / MyBatis `#{}` / PreparedStatement）。
- **依賴穩定性**：除非規格書明確要求，否則不擅自升級或新增 `pom.xml` / `build.gradle*` 的第三方依賴版本。

## 2. JUnit 5 自動化測試規範
- **驗收追蹤對齊**：每個對應規格書的測試方法請加上 `@DisplayName("[AC-XX] 情境描述")`，確保可稽核性。
- **測試層級選擇**：
  - 核心商業規則計算與分支邏輯：優先使用純 JUnit 5 + `@ExtendWith(MockitoExtension.class)` 快速驗證所有正常與異常路徑。
  - API 路由、參數驗證 (`@Valid`) 與 HTTP 狀態碼：使用 `@WebMvcTest` + `MockMvc`。
  - 複雜查詢或資料庫互動：依專案既有配置使用 `@DataJpaTest` 或整合測試基底類別。
- **斷言品質**：使用 AssertJ (`assertThat(...)`, `assertThatThrownBy(...)`) 驗證具體欄位值、狀態變更與預期的業務例外，禁止空斷言或無條件通過。

## 3. 建置與驗證指令優先序
1. 若根目錄存在 `./mvnw`：優先使用 `./mvnw -B test`（若 `pom.xml` 配置有 `checkstyle` 或 `spotless`，一併執行驗證）。
2. 若根目錄存在 `./gradlew`：優先使用 `./gradlew test check --console=plain`。
3. 否則使用容器環境內的 `mvn -B test` 或 `gradle test`。
