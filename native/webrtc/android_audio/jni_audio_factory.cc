#include <jni.h>
#include <cstdint>
#include "stereo_opus_factory.h"

static_assert(sizeof(jlong)>=sizeof(void*));
extern "C" JNIEXPORT jint JNICALL
Java_com_lumadisplay_app_LumaStereoAudioDecoderFactoryFactory_nativeAbiVersion(JNIEnv*,jclass) {
  return 1;
}
extern "C" JNIEXPORT jlong JNICALL
Java_com_lumadisplay_app_LumaStereoAudioDecoderFactoryFactory_nativeCreate(JNIEnv*,jclass) {
  auto factory=luma::CreateStereoOpusDecoderFactory();
  if(!factory||factory->GetSupportedDecoders().size()!=1)return 0;
  // Exact SDK ownership contract: native PC factory adopts this existing ref,
  // matching builtin_audio_decoder_factory_factory.cc's release() boundary.
  return static_cast<jlong>(reinterpret_cast<intptr_t>(factory.release()));
}
