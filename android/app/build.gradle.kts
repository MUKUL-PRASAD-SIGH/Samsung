plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.kotlin.serialization)
}

android {
    namespace = "com.samsung.interruptible"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.samsung.interruptible"
        minSdk = 26
        targetSdk = 34
        // CI passes -PkairosVersion=1.2.3 -PkairosVersionCode=<run number>; local builds stay 1.0 / 1.
        versionCode = (findProperty("kairosVersionCode") as String?)?.toIntOrNull() ?: 1
        versionName = (findProperty("kairosVersion") as String?) ?: "1.0"
        // Where the backend is by default: 10.0.2.2 is the host machine as seen from the Android emulator.
        buildConfigField("String", "DEFAULT_SERVER_URL", "\"ws://10.0.2.2:8000\"")
    }

    // Optional stable signing identity (CI secrets). Without it the APK is signed with the machine's auto-generated debug key, which
    // is fine to sideload but differs on every CI runner, so a newer build cannot update an older install (uninstall first).
    val keystorePath = System.getenv("KAIROS_KEYSTORE")
    if (!keystorePath.isNullOrBlank()) {
        signingConfigs {
            create("kairos") {
                storeFile = file(keystorePath)
                storePassword = System.getenv("KAIROS_KEYSTORE_PASSWORD")
                keyAlias = System.getenv("KAIROS_KEY_ALIAS") ?: "kairos"
                keyPassword = System.getenv("KAIROS_KEY_PASSWORD") ?: System.getenv("KAIROS_KEYSTORE_PASSWORD")
            }
        }
    }

    buildTypes {
        if (!keystorePath.isNullOrBlank()) {
            getByName("debug") { signingConfig = signingConfigs.getByName("kairos") }
        }
        release {
            isMinifyEnabled = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    buildFeatures {
        compose = true
        buildConfig = true
    }
    testOptions {
        unitTests.isReturnDefaultValues = true   // android.util.* stubs return defaults, so pure-Kotlin logic stays unit-testable
        unitTests.isIncludeAndroidResources = true   // Robolectric needs the merged manifest/resources for Compose UI tests
    }
    packaging { resources.excludes += "/META-INF/{AL2.0,LGPL2.1}" }
}

dependencies {
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.activity.compose)
    implementation(libs.androidx.lifecycle.viewmodel.compose)
    implementation(libs.androidx.lifecycle.runtime.compose)
    implementation(platform(libs.compose.bom))
    implementation(libs.compose.ui)
    implementation(libs.compose.ui.tooling.preview)
    implementation(libs.compose.material3)
    implementation(libs.compose.material.icons)
    implementation(libs.okhttp)
    implementation(libs.kotlinx.serialization.json)
    implementation(libs.kotlinx.coroutines.android)
    implementation(libs.camerax.core)
    implementation(libs.camerax.camera2)
    implementation(libs.camerax.lifecycle)
    implementation(libs.camerax.view)

    testImplementation(libs.junit)
    testImplementation(libs.kotlinx.coroutines.test)
    testImplementation(libs.okhttp.mockwebserver)
    testImplementation(libs.kotlinx.serialization.json)
    testImplementation(libs.robolectric)
    testImplementation(libs.androidx.test.core)
    testImplementation(platform(libs.compose.bom))
    testImplementation(libs.compose.ui.test.junit4)
    debugImplementation(libs.compose.ui.test.manifest)
}
