# GUID: 446ed7d0-e981-4580-a1a5-ec3ff810d743
import pygame

display = pygame.display
display.set_caption('Simple Pygame Example')
black = (0, 0, 0)

ball_x = 50
ball_y = 50
ball_radius = 20

ball_speed_x = 5
ball_speed_y = 5

running = True

while running:
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            running = False

    keys = pygame.key.get_pressed()

    # Move the ball
    ball_x += ball_speed_x
    ball_y += ball_speed_y

    # Bounce off the walls
    if ball_x + ball_radius > 590 or ball_x - ball_radius < 0:
        ball_speed_x = -ball_speed_x
    if ball_y + ball_radius > 590 or ball_y - ball_radius < 0:
        ball_speed_y = -ball_speed_y

    # Update the screen
    display.fill(black)
    display.blit(pygame.image.circle((ball_x, ball_y), ball_radius), (ball_x, ball_y))
    display.update()


    if not running:
        break