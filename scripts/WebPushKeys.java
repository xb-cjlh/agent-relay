import java.math.BigInteger;
import java.nio.file.*;
import java.nio.file.attribute.PosixFilePermissions;
import java.security.*;
import java.security.interfaces.ECPublicKey;
import java.security.spec.ECGenParameterSpec;
import java.util.Base64;

/** Run once with Java 17+: java scripts/WebPushKeys.java /private/path/web-push.json */
class WebPushKeys {
    static byte[] scalar(BigInteger n) {
        byte[] source=n.toByteArray(),result=new byte[32];int size=Math.min(32,source.length);
        System.arraycopy(source,source.length-size,result,32-size,size);return result;
    }
    public static void main(String[] args)throws Exception {
        if(args.length!=1)throw new IllegalArgumentException("Usage: java WebPushKeys.java /private/path/web-push.json");
        Path target=Path.of(args[0]);
        var generator=KeyPairGenerator.getInstance("EC");generator.initialize(new ECGenParameterSpec("secp256r1"));var pair=generator.generateKeyPair();
        var point=((ECPublicKey)pair.getPublic()).getW();byte[] publicKey=new byte[65];publicKey[0]=4;
        System.arraycopy(scalar(point.getAffineX()),0,publicKey,1,32);System.arraycopy(scalar(point.getAffineY()),0,publicKey,33,32);
        var b64=Base64.getUrlEncoder().withoutPadding();
        String json="{\"publicKey\":\""+b64.encodeToString(publicKey)+"\",\"privateKey\":\""+b64.encodeToString(pair.getPrivate().getEncoded())+"\"}\n";
        // CREATE_NEW prevents accidentally replacing a key used by existing subscriptions.
        try(var out=Files.newByteChannel(target,java.util.Set.of(StandardOpenOption.CREATE_NEW,StandardOpenOption.WRITE),PosixFilePermissions.asFileAttribute(PosixFilePermissions.fromString("rw-------")))) {
            var buffer=java.nio.ByteBuffer.wrap(json.getBytes(java.nio.charset.StandardCharsets.UTF_8));while(buffer.hasRemaining())out.write(buffer);
        }
        System.out.println("Created Web Push key file; retain this file across upgrades and include it in private backups.");
    }
}
