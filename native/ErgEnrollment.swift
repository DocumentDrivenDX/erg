// Standalone macOS enrollment helper. Private RSA key stays non-exportable in Keychain.
import Foundation
import Security
import CryptoKit

struct Failure: Error { let message: String }
func fail(_ text: String) throws -> Never { throw Failure(message: text) }
func b64(_ data: Data) -> String {
    data.base64EncodedString().replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: "")
}
func json(_ object: Any) throws -> Data { try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]) }
func der(_ tag: UInt8, _ body: Data) -> Data {
    var length = body.count; var size = Data()
    if length < 128 { size.append(UInt8(length)) }
    else {
        var bytes = [UInt8]()
        while length > 0 { bytes.insert(UInt8(length & 255), at: 0); length >>= 8 }
        size.append(UInt8(128 + bytes.count)); size.append(contentsOf: bytes)
    }
    return Data([tag]) + size + body
}
func seq(_ data: Data) -> Data { der(0x30, data) }
func integer(_ value: Data) -> Data {
    var bytes = value
    if bytes.first.map({$0 & 128 != 0}) ?? false { bytes.insert(0, at: 0) }
    return der(2, bytes)
}
let rsaAlgorithm = seq(der(6, Data([0x2a,0x86,0x48,0x86,0xf7,0x0d,1,1,1])) + der(5, Data()))
let signatureAlgorithm = seq(der(6, Data([0x2a,0x86,0x48,0x86,0xf7,0x0d,1,1,11])) + der(5, Data()))
func signature(_ key: SecKey, _ message: Data) throws -> Data {
    var error: Unmanaged<CFError>?
    guard let result = SecKeyCreateSignature(key, .rsaSignatureMessagePKCS1v15SHA256, message as CFData, &error) else {
        try fail("Keychain signing failed")
    }
    return result as Data
}
func findKey(_ tag: String) throws -> SecKey {
    let query: [String:Any] = [kSecClass as String:kSecClassKey, kSecAttrApplicationTag as String:Data(tag.utf8),
        kSecAttrKeyType as String:kSecAttrKeyTypeRSA, kSecAttrKeyClass as String:kSecAttrKeyClassPrivate,
        kSecReturnRef as String:true, kSecMatchLimit as String:kSecMatchLimitOne]
    var result: CFTypeRef?
    guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess, let result = result else { try fail("Enrollment key unavailable") }
    return (result as! SecKey)
}
func create(_ tag: String, _ directory: URL, days: Int = 2) throws {
    guard (1...90).contains(days) else { try fail("Certificate validity must be 1..90 days") }
    // Refuse overwrite/recreation: reinstall must recover state or use a new generation.
    guard !FileManager.default.fileExists(atPath: directory.path) else { try fail("Enrollment output already exists") }
    let query: [String:Any] = [kSecClass as String:kSecClassKey,kSecAttrApplicationTag as String:Data(tag.utf8),kSecReturnRef as String:true]
    var previous: CFTypeRef?
    guard SecItemCopyMatching(query as CFDictionary,&previous) == errSecItemNotFound else { try fail("Enrollment key tag already exists or Keychain unavailable") }
    let attributes: [String:Any] = [kSecAttrKeyType as String:kSecAttrKeyTypeRSA,kSecAttrKeySizeInBits as String:2048,
        kSecPrivateKeyAttrs as String:[kSecAttrIsPermanent as String:true,kSecAttrApplicationTag as String:Data(tag.utf8),
            kSecAttrIsExtractable as String:false,kSecAttrAccessible as String:kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly]]
    var error: Unmanaged<CFError>?
    guard let key = SecKeyCreateRandomKey(attributes as CFDictionary,&error), let publicKey = SecKeyCopyPublicKey(key),
        let publicBytes = SecKeyCopyExternalRepresentation(publicKey,&error) else { try fail("Enrollment key creation failed") }
    let now = Date(); let expiry = now.addingTimeInterval(Double(days)*86400)
    let formatter = DateFormatter(); formatter.locale = Locale(identifier:"en_US_POSIX"); formatter.timeZone = TimeZone(secondsFromGMT:0)
    formatter.dateFormat = "yyMMddHHmmss'Z'"
    let validity = seq(der(0x17,Data(formatter.string(from:now.addingTimeInterval(-300)).utf8)) + der(0x17,Data(formatter.string(from:expiry).utf8)))
    let name = seq(der(0x31,seq(der(6,Data([0x55,4,3])) + der(0x0c,Data("erg machine enrollment".utf8)))))
    var serial = Data(count:16)
    let status = serial.withUnsafeMutableBytes { SecRandomCopyBytes(kSecRandomDefault,16,$0.baseAddress!) }
    guard status == errSecSuccess else { try fail("Random generation failed") }
    let tbs = seq(der(0xa0,integer(Data([2]))) + integer(serial) + signatureAlgorithm + name + validity + name + seq(rsaAlgorithm + der(3,Data([0]) + (publicBytes as Data))))
    let certificate = seq(tbs + signatureAlgorithm + der(3,Data([0]) + (try signature(key,tbs))))
    guard SecCertificateCreateWithData(nil,certificate as CFData) != nil else { try fail("Certificate encoding failed") }
    try FileManager.default.createDirectory(at:directory,withIntermediateDirectories:true,attributes:[.posixPermissions:0o700])
    let lines = certificate.base64EncodedString(options:[.lineLength64Characters,.endLineWithLineFeed])
    try Data(("-----BEGIN CERTIFICATE-----\n"+lines+"\n-----END CERTIFICATE-----\n").utf8).write(to:directory.appendingPathComponent("certificate.pem"),options:.atomic)
    let metadata: [String:Any] = ["key_tag":tag,"private_key_exported":false,"certificate_expires_at":ISO8601DateFormatter().string(from:expiry),
        "certificate_sha1":Insecure.SHA1.hash(data:certificate).map{String(format:"%02x",$0)}.joined(),"certificate_sha256":SHA256.hash(data:certificate).map{String(format:"%02x",$0)}.joined()]
    try json(metadata).write(to:directory.appendingPathComponent("key-metadata.json"),options:.atomic)
    print(String(data:try json(metadata),encoding:.utf8)!)
}
func certificate(_ path: String) throws -> Data {
    let pem = try String(contentsOfFile:path,encoding:.utf8)
    let lines = pem.split(separator:"\n").filter{ !$0.hasPrefix("-----") }.joined()
    guard let data = Data(base64Encoded:lines),SecCertificateCreateWithData(nil,data as CFData) != nil else { try fail("Invalid public certificate") }
    return data
}
func assertion(_ tag: String, _ certificatePath: String, _ tenant: String, _ client: String) throws -> String {
    guard UUID(uuidString:tenant) != nil,UUID(uuidString:client) != nil else { try fail("Invalid tenant/client ID") }
    let cert = try certificate(certificatePath); let key = try findKey(tag)
    guard let publicKey = SecKeyCopyPublicKey(key),let certObject = SecCertificateCreateWithData(nil,cert as CFData),let certKey = SecCertificateCopyKey(certObject) else { try fail("Public key unavailable") }
    var error: Unmanaged<CFError>?
    guard SecKeyCopyExternalRepresentation(publicKey,&error) == SecKeyCopyExternalRepresentation(certKey,&error) else { try fail("Certificate/key mismatch") }
    let header: [String:Any] = ["alg":"RS256","typ":"JWT","x5t":b64(Data(Insecure.SHA1.hash(data:cert)))]
    let now = Int(Date().timeIntervalSince1970)
    let payload: [String:Any] = ["aud":"https://login.microsoftonline.com/"+tenant+"/oauth2/v2.0/token",
        "iss":client,"sub":client,"jti":UUID().uuidString.lowercased(),"iat":now,"nbf":now-30,"exp":now+300]
    let content = b64(try json(header))+"."+b64(try json(payload))
    return content+"."+b64(try signature(key,Data(content.utf8)))
}
final class NoRedirect: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
}
func token(_ tag: String, _ cert: String, _ tenant: String, _ client: String, _ audience: String) throws {
    guard UUID(uuidString:audience) != nil else { try fail("Invalid API audience") }
    let signed = try assertion(tag,cert,tenant,client)
    var form = URLComponents()
    form.queryItems = [URLQueryItem(name:"grant_type",value:"client_credentials"),URLQueryItem(name:"client_id",value:client),
        URLQueryItem(name:"scope",value:"api://"+audience+"/.default"),
        URLQueryItem(name:"client_assertion_type",value:"urn:ietf:params:oauth:client-assertion-type:jwt-bearer"),
        URLQueryItem(name:"client_assertion",value:signed)]
    var request = URLRequest(url:URL(string:"https://login.microsoftonline.com/"+tenant+"/oauth2/v2.0/token")!)
    request.httpMethod = "POST"; request.timeoutInterval = 20
    request.setValue("application/x-www-form-urlencoded",forHTTPHeaderField:"Content-Type")
    // URLComponents uses RFC3986; escape literal plus so form decoding preserves it.
    request.httpBody = form.percentEncodedQuery!.replacingOccurrences(of:"+",with:"%2B").data(using:.utf8)
    let signal = DispatchSemaphore(value:0)
    var received: Data?; var code = 0
    let session = URLSession(configuration:.ephemeral,delegate:NoRedirect(),delegateQueue:nil)
    let task = session.dataTask(with:request) { data,response,error in
        if error == nil { received = data; code = (response as? HTTPURLResponse)?.statusCode ?? 0 }; signal.signal()
    }
    task.resume()
    guard signal.wait(timeout:.now()+25) == .success else { task.cancel(); try fail("Token exchange timed out") }
    session.invalidateAndCancel()
    guard code == 200,let data = received,data.count <= 65536,
        let value = try JSONSerialization.jsonObject(with:data) as? [String:Any],let access = value["access_token"] as? String,
        !access.isEmpty,access.count <= 16384,!access.contains(where:{$0.isWhitespace}) else { try fail("Token exchange rejected") }
    print(access) // Only token command stdout is secret; use an in-memory pipe, never a terminal/log.
}

do {
    let args = Array(CommandLine.arguments.dropFirst())
    guard !args.isEmpty else { try fail("Usage: create-key TAG NEW_OUTPUT_DIR | assertion TAG PUBLIC_CERT TENANT CLIENT | token TAG PUBLIC_CERT TENANT CLIENT API_AUDIENCE") }
    if args[0] == "create-key" && args.count == 3 { try create(args[1],URL(fileURLWithPath:args[2])) }
    else if args[0] == "create-key" && args.count == 4, let days = Int(args[3]) { try create(args[1],URL(fileURLWithPath:args[2]),days:days) }
    else if args[0] == "assertion" && args.count == 5 { print(try assertion(args[1],args[2],args[3],args[4])) }
    else if args[0] == "token" && args.count == 6 { try token(args[1],args[2],args[3],args[4],args[5]) }
    else { try fail("Invalid enrollment command") }
} catch let error as Failure { FileHandle.standardError.write(Data((error.message+"\n").utf8)); exit(1) }
catch { FileHandle.standardError.write(Data("Enrollment operation failed\n".utf8)); exit(1) }
