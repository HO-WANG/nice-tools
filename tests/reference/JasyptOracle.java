import org.jasypt.encryption.pbe.StandardPBEStringEncryptor;
import org.jasypt.salt.RandomSaltGenerator;
import org.jasypt.salt.ZeroSaltGenerator;
import org.jasypt.iv.RandomIvGenerator;
import org.jasypt.iv.NoIvGenerator;
import java.util.Base64;
import java.nio.charset.StandardCharsets;

class Oracle {
 public static void main(String[] args) {
  try {
   StandardPBEStringEncryptor e = new StandardPBEStringEncryptor();
   e.setPassword(new String(Base64.getDecoder().decode(args[2]), StandardCharsets.UTF_8));
   e.setAlgorithm(args[1]);
   e.setKeyObtentionIterations(Integer.parseInt(args[3]));
   e.setSaltGenerator(args[4].equals("random") ? new RandomSaltGenerator() : new ZeroSaltGenerator());
   e.setIvGenerator(args[5].equals("random") ? new RandomIvGenerator() : new NoIvGenerator());
   e.setStringOutputType(args[6]);
   String input = new String(Base64.getDecoder().decode(args[7]), StandardCharsets.UTF_8);
   String result = args[0].equals("encrypt") ? e.encrypt(input) : e.decrypt(input);
   System.out.println(Base64.getEncoder().encodeToString(result.getBytes(StandardCharsets.UTF_8)));
  } catch (Exception ex) {
   System.err.println(ex.getClass().getName()+": "+ex.getMessage());
   System.exit(1);
  }
 }
}
