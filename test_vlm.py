import cv2
from models.vlm import analyze_image

image = cv2.imread("test_images/test.jpeg")

while True:

    question = input("User >> ")

    if question == "q":
        break

    answer = analyze_image(image, question)

    print("SENSE+ >>", answer)