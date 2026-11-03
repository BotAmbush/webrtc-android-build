#ifndef LUMA_STEREO_OPUS_FACTORY_H_
#define LUMA_STEREO_OPUS_FACTORY_H_

#include "api/audio_codecs/audio_decoder_factory.h"
#include "api/audio_codecs/audio_format.h"
#include "api/scoped_refptr.h"

namespace luma {
// Receiver preferences consumed by the supported WebRTC codec-factory API.
// No SDP text rewriting, device creation, JNI state or platform audio lives here.
webrtc::SdpAudioFormat StereoOpusReceiveFormat();
webrtc::scoped_refptr<webrtc::AudioDecoderFactory> CreateStereoOpusDecoderFactory();
}
#endif
