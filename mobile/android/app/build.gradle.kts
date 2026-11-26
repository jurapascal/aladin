plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "io.github.itbrno.claudehub"
    compileSdk = 35

    defaultConfig {
        applicationId = "io.github.itbrno.claudehub"
        minSdk = 26                 // WebView s ES2020, tedy vše, co hub v JS používá
        targetSdk = 35
        versionCode = 2
        versionName = "2.1.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("debug")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    buildFeatures { viewBinding = false }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.activity:activity-ktx:1.9.3")
    // Čtečka QR bez Google Play services — telefon nemusí mít Play Store.
    implementation("com.journeyapps:zxing-android-embedded:4.3.0")
}
