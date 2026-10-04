# kotlinx.serialization keeps its generated serializers via the plugin; OkHttp/Okio ship consumer rules.
-keepattributes *Annotation*, InnerClasses
-dontwarn org.bouncycastle.**
-dontwarn org.conscrypt.**
-dontwarn org.openjsse.**
