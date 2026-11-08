package dev.relay;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.time.Instant;
import java.util.*;

final class Values {
    static final int MAX_FILE = 16 * 1024 * 1024;
    static final int MAX_BODY = 23 * 1024 * 1024;
    static final int MAX_OUTPUT = 64000;
    static final Set<String> TERMINAL = Set.of("succeeded", "failed", "cancelled");
    private static final SecureRandom RANDOM = new SecureRandom();

    static double now() {
        Instant instant = Instant.now();
        return instant.getEpochSecond() + instant.getNano() / 1_000_000_000.0;
    }

    static Map<String, Object> map(Object... entries) {
        Map<String, Object> result = new LinkedHashMap<>();
        for (int i = 0; i < entries.length; i += 2) result.put((String) entries[i], entries[i + 1]);
        return result;
    }

    static String required(Map<String, Object> body, String key, int limit) {
        Object value = body.get(key);
        if (!(value instanceof String text) || text.isBlank() || text.codePointCount(0, text.length()) > limit || text.indexOf('\0') >= 0)
            throw new ApiException("字段无效: " + key);
        return text.strip();
    }

    static String required(Map<String, Object> body, String key) { return required(body, key, 4096); }

    @SuppressWarnings("unchecked")
    static Map<String, Object> object(Object value) {
        if (!(value instanceof Map<?, ?>)) throw new ApiException("请求字段必须是对象");
        return (Map<String, Object>) value;
    }

    @SuppressWarnings("unchecked")
    static List<Object> list(Object value, int max) {
        if (!(value instanceof List<?> values) || values.size() > max) throw new ApiException("列表格式或长度无效");
        return (List<Object>) values;
    }

    static List<Object> roots(Object value) {
        List<Object> roots = list(value, 100);
        if (roots.stream().anyMatch(p -> !(p instanceof String s) || s.isBlank() || s.length() > 4096))
            throw new ApiException("操作目录格式无效");
        return roots;
    }

    static String machineId(Object value) {
        if (!(value instanceof String id) || !id.matches("[0-9a-f]{64}")) throw new ApiException("本机标识无效");
        return id;
    }

    static int integer(Object value, int min, int max, String field) {
        if (!(value instanceof Integer || value instanceof Long) || ((Number) value).longValue() < min || ((Number) value).longValue() > max)
            throw new ApiException(field + " 必须介于 " + min + " 和 " + max);
        return ((Number) value).intValue();
    }

    static double number(Object value) {
        if (!(value instanceof Number n) || !Double.isFinite(n.doubleValue())) throw new ApiException("时间格式无效");
        return n.doubleValue();
    }

    static boolean truth(Object value) {
        return Boolean.TRUE.equals(value) || (value instanceof Number n && n.doubleValue() != 0);
    }

    static String text(Object value) {
        if (!(value instanceof String s)) throw new ApiException("文本格式无效");
        return s;
    }

    static String tail(String value, int limit) { int size = value.codePointCount(0,value.length()); return value.substring(value.offsetByCodePoints(0,Math.max(0,size-limit))); }
    static String head(String value, int limit) { return value.substring(0,value.offsetByCodePoints(0,Math.min(value.codePointCount(0,value.length()),limit))); }
    static String id(String prefix) { return prefix + "_" + UUID.randomUUID().toString().replace("-", "").substring(0, 20); }

    static String token() {
        byte[] bytes = new byte[32]; RANDOM.nextBytes(bytes);
        return Base64.getUrlEncoder().withoutPadding().encodeToString(bytes);
    }

    static String hash(String value) { return hash(value.getBytes(StandardCharsets.UTF_8)); }
    static String hash(byte[] value) {
        try { return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(value)); }
        catch (java.security.NoSuchAlgorithmException e) { throw new IllegalStateException(e); }
    }

    static boolean secureEquals(String a, String b) {
        return MessageDigest.isEqual(a.getBytes(StandardCharsets.UTF_8), b.getBytes(StandardCharsets.UTF_8));
    }

    static String sessionId(Object value) {
        if (!(value instanceof String id) || id.length() > 200 || !id.matches("[a-zA-Z0-9_][a-zA-Z0-9_-]*"))
            throw new ApiException("会话 ID 无效");
        return id;
    }
}
