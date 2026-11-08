package dev.relay;

final class ApiException extends RuntimeException {
    final int status;

    ApiException(String message) { this(400, message); }

    ApiException(int status, String message) {
        super(message);
        this.status = status;
    }
}
